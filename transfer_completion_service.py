"""Unified business completion contract for Transfer Cases.

Readiness is based only on the final-school Offer, a paid Payment Receipt,
and the final-school COE. Optional follow-up arrangements remain outside this
contract. New files are never handled here; this service reads only canonical
records and current persisted links created after Upload Review.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Collection, Mapping

from security import now_iso
import student_case_repository
import transfer_case_repository
from student_lifecycle.module_progress import load_cycle_module_progress


TERMINAL_APPLICATION_STATUSES = frozenset(
    {"rejected", "withdrawn", "cancelled"}
)
TERMINAL_OFFER_STATUSES = frozenset(
    {"rejected", "withdrawn", "cancelled", "canceled", "expired", "void"}
)

COMPLETION_PROBLEM_LABELS = {
    "case_not_active": "转学记录当前不是可完成状态",
    "final_school": "尚未确认最终学校",
    "final_offer": "缺少最终学校Offer",
    "final_coe": "缺少最终学校COE",
    "payment_receipt": "缺少付款证明",
    "offer_record_invalid": "Offer业务记录无效",
    "offer_record_terminal": "Offer业务记录已失效",
    "offer_file_not_available": "Offer正式文件不存在",
    "offer_upload_review_pending": "Offer尚未通过上传审核",
    "coe_record_invalid": "COE业务记录无效",
    "coe_file_not_available": "COE正式文件不存在",
    "coe_upload_review_pending": "COE尚未通过上传审核",
    "payment_record_invalid": "付款业务记录无效",
    "payment_file_not_available": "付款证明正式文件不存在",
    "payment_upload_review_pending": "付款证明尚未通过上传审核",
    "payment_not_paid": "付款状态尚未标记为已支付",
    "final_school_identity_missing": "最终学校资料不完整",
    "offer_final_school_mismatch": "Offer与最终学校不一致",
    "coe_final_school_mismatch": "COE与最终学校不一致",
    "offer_coe_school_mismatch": "Offer与COE所属学校不一致",
}


@dataclass(frozen=True)
class TransferCompletionEvaluation:
    case_id: int
    student_id: str
    is_ready_to_complete: bool
    missing_requirements: tuple[str, ...]
    inconsistent_requirements: tuple[str, ...]
    final_school_application_id: int | None
    offer_record_id: int | None
    payment_record_id: int | None
    coe_record_id: int | None
    payment_status: str | None


@dataclass(frozen=True)
class TransferCompletionResult:
    case_id: int
    confirmation_id: int
    event_id: int | None
    completed_at: str
    completed_by: int
    idempotent: bool


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normal_school(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _existing_file(*values: Any) -> bool:
    for value in values:
        raw = str(value or "").strip()
        if raw and Path(raw).is_file():
            return True
    return False


def _row(connection: sqlite3.Connection, query: str, parameters=()):
    result = connection.execute(query, parameters).fetchone()
    return None if result is None else dict(result)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')")
    }


def _confirmed_upload_review(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    module: str,
    file_paths: Collection[Any],
) -> bool:
    paths = tuple(
        str(value).strip() for value in file_paths if str(value or "").strip()
    )
    if not paths or not _columns(connection, "upload_review_items"):
        return False
    placeholders = ",".join("?" for _ in paths)
    row = connection.execute(
        f"""SELECT 1 FROM upload_review_items
             WHERE student_id=? AND module_key=? AND status='Confirmed'
               AND confirmed_at IS NOT NULL
               AND final_file_path IN ({placeholders})
             LIMIT 1""",
        (str(student_id), module, *paths),
    ).fetchone()
    return row is not None


def _selected(columns: set[str], required: tuple[str, ...], optional: tuple[str, ...]):
    return ",".join((*required, *(name for name in optional if name in columns)))


class TransferCompletionService:
    def __init__(self, database_path: str | Path, *, clock=now_iso, timeout=5.0):
        self.database_path = Path(database_path).resolve()
        self.clock = clock
        self.timeout = float(timeout)

    def _connect(self):
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _evaluate(self, connection: sqlite3.Connection, case_id: int):
        case = _row(
            connection,
            """SELECT sc.id AS case_id,sc.student_id,sc.status,
                      sc.terminal_result,sc.active_slot,sc.completed_at,
                      sc.paused_at,sc.cancelled_at,sc.failed_at,sc.archived_at,
                      sc.created_by,sc.created_at,sc.updated_by,sc.updated_at,
                      tc.workflow_state,tc.final_application_id,
                      tc.current_school_enrolment_id,
                      tc.upcoming_school_enrolment_id,tc.relationship_status
               FROM student_cases sc
               JOIN transfer_cases tc ON tc.case_id=sc.id
               WHERE sc.id=? AND sc.case_type='transfer'""",
            (int(case_id),),
        )
        if case is None:
            raise ValueError("转学记录不存在")

        payment_disposition = None
        if connection.execute(
            """SELECT 1 FROM sqlite_master
                WHERE type='table'
                  AND name='lifecycle_module_disposition_events'"""
        ).fetchone() is not None:
            cycle = connection.execute(
                """SELECT id FROM application_cycles
                     WHERE source_student_case_id=? AND cycle_type='transfer'
                     ORDER BY id DESC LIMIT 1""",
                (int(case_id),),
            ).fetchone()
            if cycle is not None:
                payment_disposition = next(
                    item
                    for item in load_cycle_module_progress(
                        connection, int(cycle[0])
                    )
                    if item.module_key == "payments"
                )
        payment_exempt = bool(
            payment_disposition is not None
            and payment_disposition.satisfied
            and payment_disposition.disposition
            in {"existing", "not_applicable"}
        )

        missing: list[str] = []
        inconsistent: list[str] = []
        if (
            int(case["active_slot"]) != 1
            or case["status"] in {"paused", "completed", "cancelled", "failed", "archived"}
            or case["terminal_result"] is not None
        ):
            inconsistent.append("case_not_active")

        final = None
        final_id = case["final_application_id"]
        if final_id is not None:
            final = _row(
                connection,
                """SELECT id AS application_id,school_id,school_name_snapshot,
                          application_status,is_final
                   FROM transfer_school_applications
                   WHERE id=? AND case_id=?""",
                (int(final_id), int(case_id)),
            )
        if (
            final is None
            or int(final.get("is_final") or 0) != 1
            or str(final.get("application_status") or "").lower()
            in TERMINAL_APPLICATION_STATUSES
        ):
            missing.append("final_school")
            final = None
            final_id = None

        offer_record_id = None
        coe_record_id = None
        payment_record_id = None
        payment_status = None
        offer = None
        coe = None
        payment = None

        if final_id is not None:
            offer_link = _row(
                connection,
                """SELECT * FROM case_record_links
                   WHERE case_id=? AND transfer_application_id=?
                     AND record_type='offer' AND validity_status='current'
                   ORDER BY effective_from DESC,id DESC LIMIT 1""",
                (int(case_id), int(final_id)),
            )
            if offer_link is None:
                missing.append("final_offer")
            else:
                offer_record_id = int(offer_link["record_id"])
                columns = _columns(connection, "offers")
                offer = _row(
                    connection,
                    f"""SELECT {_selected(columns, ('id','student_id'), ('school_name','offer_status','offer_file_path','signed_offer_file_path'))}
                        FROM offers WHERE id=?""",
                    (offer_record_id,),
                )
                if offer is None or str(offer["student_id"]) != str(case["student_id"]):
                    inconsistent.append("offer_record_invalid")
                else:
                    status = str(offer.get("offer_status") or "").lower()
                    if status in TERMINAL_OFFER_STATUSES:
                        inconsistent.append("offer_record_terminal")
                    offer_paths = (
                        offer.get("offer_file_path"),
                        offer.get("signed_offer_file_path"),
                    )
                    if not _existing_file(*offer_paths):
                        inconsistent.append("offer_file_not_available")
                    if not _confirmed_upload_review(
                        connection,
                        student_id=case["student_id"],
                        module="Offer",
                        file_paths=offer_paths,
                    ):
                        inconsistent.append("offer_upload_review_pending")

            coe_link = _row(
                connection,
                """SELECT * FROM case_record_links
                   WHERE case_id=? AND transfer_application_id=?
                     AND record_type='coe' AND validity_status='current'
                   ORDER BY effective_from DESC,id DESC LIMIT 1""",
                (int(case_id), int(final_id)),
            )
            if coe_link is None:
                missing.append("final_coe")
            else:
                coe_record_id = int(coe_link["record_id"])
                columns = _columns(connection, "coe_oshc")
                coe = _row(
                    connection,
                    f"""SELECT {_selected(columns, ('id','student_id','document_kind'), ('school_name','coe_provider','file_path'))}
                        FROM coe_oshc WHERE id=?""",
                    (coe_record_id,),
                )
                if (
                    coe is None
                    or str(coe["student_id"]) != str(case["student_id"])
                    or coe["document_kind"] not in {"COE", "COE_CAAW"}
                ):
                    inconsistent.append("coe_record_invalid")
                else:
                    if not _existing_file(coe.get("file_path")):
                        inconsistent.append("coe_file_not_available")
                    if not _confirmed_upload_review(
                        connection,
                        student_id=case["student_id"],
                        module="COE + OSHC",
                        file_paths=(coe.get("file_path"),),
                    ):
                        inconsistent.append("coe_upload_review_pending")

            proof = _row(
                connection,
                """SELECT requirement.status AS payment_status,
                          requirement.id AS requirement_id,
                          link.record_id AS payment_record_id,
                          link.record_type,link.validity_status AS link_status,
                          proof.validity_status AS proof_status
                   FROM case_payment_requirements requirement
                   JOIN case_payment_requirement_proofs proof
                     ON proof.payment_requirement_id=requirement.id
                    AND proof.case_id=requirement.case_id
                    AND proof.transfer_application_id=requirement.transfer_application_id
                   JOIN case_record_links link ON link.id=proof.case_record_link_id
                   WHERE requirement.case_id=?
                     AND requirement.transfer_application_id=?
                     AND requirement.requirement_type IN ('deposit','first_payment','other')
                     AND proof.validity_status='current'
                     AND link.validity_status='current'
                     AND link.record_type='payment'
                   ORDER BY requirement.updated_at DESC,proof.id DESC LIMIT 1""",
                (int(case_id), int(final_id)),
            )
            if proof is None and not payment_exempt:
                missing.append("payment_receipt")
            elif proof is not None:
                payment_record_id = int(proof["payment_record_id"])
                payment_status = str(proof["payment_status"] or "")
                columns = _columns(connection, "payments")
                payment = _row(
                    connection,
                    f"""SELECT {_selected(columns, ('id','student_id'), ('receipt_file_path',))}
                        FROM payments WHERE id=?""",
                    (payment_record_id,),
                )
                if payment is None or str(payment["student_id"]) != str(case["student_id"]):
                    inconsistent.append("payment_record_invalid")
                else:
                    if not _existing_file(payment.get("receipt_file_path")):
                        inconsistent.append("payment_file_not_available")
                    if not _confirmed_upload_review(
                        connection,
                        student_id=case["student_id"],
                        module="Payments",
                        file_paths=(payment.get("receipt_file_path"),),
                    ):
                        inconsistent.append("payment_upload_review_pending")
                if payment_status != "paid":
                    inconsistent.append("payment_not_paid")
            elif payment_disposition is not None:
                payment_status = payment_disposition.disposition

        if final is not None:
            final_name = _normal_school(final.get("school_name_snapshot"))
            if not final_name:
                inconsistent.append("final_school_identity_missing")
            if offer is not None:
                offer_name = _normal_school(offer.get("school_name"))
                if not offer_name or offer_name != final_name:
                    inconsistent.append("offer_final_school_mismatch")
            if coe is not None:
                coe_name = _normal_school(
                    coe.get("school_name") or coe.get("coe_provider")
                )
                if not coe_name or coe_name != final_name:
                    inconsistent.append("coe_final_school_mismatch")
            if offer is not None and coe is not None:
                if _normal_school(offer.get("school_name")) != _normal_school(
                    coe.get("school_name") or coe.get("coe_provider")
                ):
                    inconsistent.append("offer_coe_school_mismatch")

        missing = list(dict.fromkeys(missing))
        inconsistent = list(dict.fromkeys(inconsistent))
        return TransferCompletionEvaluation(
            case_id=int(case_id),
            student_id=str(case["student_id"]),
            is_ready_to_complete=not missing and not inconsistent,
            missing_requirements=tuple(missing),
            inconsistent_requirements=tuple(inconsistent),
            final_school_application_id=(None if final_id is None else int(final_id)),
            offer_record_id=offer_record_id,
            payment_record_id=payment_record_id,
            coe_record_id=coe_record_id,
            payment_status=payment_status,
        )

    def evaluate_transfer_completion(self, case_id: int):
        with self._connect() as connection:
            return self._evaluate(connection, int(case_id))

    @staticmethod
    def _event_values(case_id, actor_user_id, timestamp, request_id, evaluation):
        payload = asdict(evaluation)
        return {
            "case_id": int(case_id),
            "event_type": "transfer_completion_completed",
            "entity_type": "transfer_case",
            "entity_id": str(case_id),
            "old_value_json": _canonical_json({"status": "ready_to_complete"}),
            "new_value_json": _canonical_json({"status": "completed"}),
            "metadata_json": _canonical_json({
                "actor_user_id": int(actor_user_id),
                "completion_contract": "final-offer-paid-payment-final-coe-v1",
                "evaluation": payload,
            }),
            "actor_type": "user",
            "actor_user_id": int(actor_user_id),
            "request_id": request_id,
            "correlation_id": None,
            "correction_of_event_id": None,
            "created_at": timestamp,
        }

    def complete_transfer_case(
        self,
        case_id: int,
        actor_user_id: int,
        *,
        actor_permissions: Collection[str] | None = None,
        completion_enabled: bool = False,
        idempotency_key: str | None = None,
    ) -> TransferCompletionResult:
        permissions = set(actor_permissions or ())
        if not completion_enabled or "student_cases.complete" not in permissions:
            raise PermissionError("无权限确认转学完成")
        key = str(idempotency_key or f"transfer-core-complete:{int(case_id)}").strip()
        if not key:
            raise ValueError("完成请求标识不能为空")
        timestamp = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                actor = _row(
                    connection,
                    "SELECT id,is_active FROM users WHERE id=?",
                    (int(actor_user_id),),
                )
                if actor is None or int(actor.get("is_active", 0)) != 1:
                    raise PermissionError("完成操作人无效")
                existing = transfer_case_repository.get_latest_open_completion(
                    connection, int(case_id)
                )
                if existing is not None:
                    event = _row(
                        connection,
                        """SELECT id FROM case_events WHERE case_id=?
                           AND event_type='transfer_completion_completed'
                           ORDER BY id DESC LIMIT 1""",
                        (int(case_id),),
                    )
                    connection.rollback()
                    return TransferCompletionResult(
                        case_id=int(case_id),
                        confirmation_id=int(existing["id"]),
                        event_id=None if event is None else int(event["id"]),
                        completed_at=str(existing["confirmed_at"]),
                        completed_by=int(existing["actor_user_id"]),
                        idempotent=True,
                    )

                evaluation = self._evaluate(connection, int(case_id))
                if not evaluation.is_ready_to_complete:
                    raise ValueError(
                        "转学完成条件未满足："
                        + "、".join(
                            COMPLETION_PROBLEM_LABELS.get(code, "核心记录不完整")
                            for code in (
                                *evaluation.missing_requirements,
                                *evaluation.inconsistent_requirements,
                            )
                        )
                    )
                case = _row(
                    connection,
                    """SELECT sc.*,tc.workflow_state,
                              tc.current_school_enrolment_id,
                              tc.upcoming_school_enrolment_id,
                              tc.relationship_status
                       FROM student_cases sc
                       JOIN transfer_cases tc ON tc.case_id=sc.id
                       WHERE sc.id=?""",
                    (int(case_id),),
                )
                if case is None:
                    raise ValueError("转学记录不存在")
                if case["workflow_state"] != "ready_to_complete":
                    if not transfer_case_repository.update_transfer_workflow(
                        connection,
                        case_id=int(case_id),
                        workflow_state="ready_to_complete",
                        updated_at=timestamp,
                    ):
                        raise ValueError("无法更新转学完成准备状态")
                if not student_case_repository.update_case_status(
                    connection,
                    int(case_id),
                    {
                        "status": "completed",
                        "terminal_result": "completed",
                        "active_slot": 0,
                        "paused_at": case["paused_at"],
                        "completed_at": timestamp,
                        "cancelled_at": case["cancelled_at"],
                        "failed_at": case["failed_at"],
                        "archived_at": case["archived_at"],
                        "updated_by": int(actor_user_id),
                        "updated_at": timestamp,
                    },
                ):
                    raise ValueError("无法更新转学完成状态")
                if not transfer_case_repository.update_transfer_workflow(
                    connection,
                    case_id=int(case_id),
                    workflow_state="completed",
                    updated_at=timestamp,
                ):
                    raise ValueError("无法更新转学流程状态")

                school_projection = {
                    "current_school_enrolment_id": case["current_school_enrolment_id"],
                    "upcoming_school_enrolment_id": case["upcoming_school_enrolment_id"],
                    "relationship_status": case["relationship_status"],
                }
                empty_snapshot = _canonical_json({})
                readiness_json = _canonical_json(asdict(evaluation))
                confirmation_id = transfer_case_repository.insert_completion_confirmation(
                    connection,
                    {
                        "case_id": int(case_id),
                        "action": "complete",
                        "reverses_confirmation_id": None,
                        "actor_user_id": int(actor_user_id),
                        "confirmed_at": timestamp,
                        "reason": "核心文件完成条件已由员工确认",
                        "effective_date": str(timestamp)[:10],
                        "idempotency_key": key,
                        "workflow_state_before": "ready_to_complete",
                        "workflow_state_after": "completed",
                        "case_status_before": str(case["status"]),
                        "case_status_after": "completed",
                        "active_slot_before": int(case["active_slot"]),
                        "active_slot_after": 0,
                        "terminal_result_before": case["terminal_result"],
                        "terminal_result_after": "completed",
                        "current_enrolment_before_json": empty_snapshot,
                        "current_enrolment_after_json": empty_snapshot,
                        "upcoming_enrolment_before_json": empty_snapshot,
                        "upcoming_enrolment_after_json": empty_snapshot,
                        "readiness_snapshot_json": readiness_json,
                        "school_state_before_json": _canonical_json(school_projection),
                        "school_state_after_json": _canonical_json(school_projection),
                        "metadata_json": _canonical_json({
                            "completion_contract": "final-offer-paid-payment-final-coe-v1",
                            "evaluation": asdict(evaluation),
                            "original_workflow_state": case["workflow_state"],
                        }),
                        "created_at": timestamp,
                    },
                )
                event_id = student_case_repository.append_event(
                    connection,
                    self._event_values(
                        case_id, actor_user_id, timestamp, key, evaluation
                    ),
                )
                connection.commit()
                return TransferCompletionResult(
                    case_id=int(case_id),
                    confirmation_id=int(confirmation_id),
                    event_id=int(event_id),
                    completed_at=str(timestamp),
                    completed_by=int(actor_user_id),
                    idempotent=False,
                )
            except Exception:
                connection.rollback()
                raise


def evaluate_transfer_completion(database_path: str | Path, case_id: int):
    return TransferCompletionService(database_path).evaluate_transfer_completion(case_id)


def complete_transfer_case(
    database_path: str | Path,
    case_id: int,
    actor_user_id: int,
    **kwargs,
):
    return TransferCompletionService(database_path).complete_transfer_case(
        case_id, actor_user_id, **kwargs
    )
