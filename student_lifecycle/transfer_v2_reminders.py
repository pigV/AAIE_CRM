from __future__ import annotations

import json
from typing import Iterable

from reminder_center import create_from_event
from .transfer_v2_cutover import cutover_applied_at, is_new_transfer_cycle
from .transfer_v2_projection import load_transfer_v2_projection


TERMINAL_REMINDER_STATUSES = {"Completed", "Dismissed", "Cancelled"}
BLOCKER_AREAS = {
    "materials_preparation": ("files", "补齐本次转学材料"),
    "school_plan": ("branches", "确认目标学校方案"),
    "school_application": ("branches", "更新目标学校申请"),
    "offer_received": ("offers", "确认已签署录取通知并完成付款确认"),
    "accepted_and_paid": ("offers", "选择当前接受学校"),
    "coe_update": ("coe", "更新并人工确认入学确认文件"),
    "pending_completion": ("overview", "确认结束转学"),
    "offer_accepted": ("payments", "确认 Accepted Offer 付款"),
    "paid": ("overview", "明确进入等待 COE"),
    "waiting_coe": ("coe", "上传并人工确认 COE"),
    "coe_received": ("overview", "确认完成申请"),
    "application_complete": ("overview", "完成转学"),
}


def _columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')")}


def _event_anchor(connection, cycle_id: int) -> str:
    values = [f"cycle-{int(cycle_id)}"]
    queries = [
        ("target", "SELECT max(id) FROM transfer_target_state_events WHERE application_cycle_id=?"),
        ("offer", """SELECT max(state.id) FROM offer_received_event_state_events AS state
                         JOIN offer_received_events AS offer
                           ON offer.id=state.offer_received_event_id
                        WHERE offer.application_cycle_id=?"""),
        ("acceptance", "SELECT max(id) FROM cycle_final_acceptances WHERE application_cycle_id=?"),
        ("payment_record", """SELECT max(link.id) FROM case_record_links AS link
                                  JOIN transfer_school_applications AS branch
                                    ON branch.id=link.transfer_application_id
                                 WHERE branch.application_cycle_id=?
                                   AND link.record_type='payment'
                                   AND link.validity_status IN ('current','carried_forward')"""),
        ("coe", """SELECT max(event.id) FROM transfer_coe_events AS event
                       JOIN transfer_coe_confirmations AS confirmation
                         ON confirmation.id=event.confirmation_id
                      WHERE confirmation.application_cycle_id=?"""),
        ("lifecycle", "SELECT max(id) FROM transfer_v2_lifecycle_events WHERE application_cycle_id=?"),
        ("completion", "SELECT max(id) FROM transfer_completion_events WHERE application_cycle_id=?"),
    ]
    if not is_new_transfer_cycle(connection, int(cycle_id)):
        queries.append(
            ("payment", """SELECT max(event.id) FROM transfer_payment_events AS event
                               JOIN transfer_payment_aggregates AS payment
                                 ON payment.id=event.payment_id
                              WHERE payment.application_cycle_id=?""")
        )
    for label, sql in queries:
        try:
            row = connection.execute(sql, (int(cycle_id),)).fetchone()
        except Exception:
            continue
        if row is not None and row[0] is not None:
            values.append(f"{label}-{int(row[0])}")
    return "-".join(values)


def _deep_link(student_id: str, case_id: int, area: str, blocker_key: str) -> str:
    return (
        f"/students/{student_id}/transfer-cases/{int(case_id)}"
        f"?highlight={blocker_key}#transfer-v2-{area}"
    )


def _close_reminders(database, rows, *, now: str, reason: str) -> int:
    changed = 0
    if not rows:
        return changed
    with database.transaction() as connection:
        for row in rows:
            if str(row["status"] or "Pending") in TERMINAL_REMINDER_STATUSES:
                continue
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            metadata.update({"auto_close_reason": reason, "auto_closed_at": now})
            notes = str(row["notes"] or "").strip()
            auto_note = f"Auto-closed: {reason} at {now}"
            connection.execute(
                """UPDATE reminders
                      SET status='Completed',completed_at=?,completed_by=NULL,
                          snooze_until=NULL,updated_at=?,metadata_json=?,notes=?
                    WHERE id=? AND status NOT IN ('Completed','Dismissed','Cancelled')""",
                (
                    now,
                    now,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    f"{notes}\n{auto_note}".strip(),
                    int(row["id"]),
                ),
            )
            changed += 1
    return changed


def _active_rows(database, cycle_id: int, event_type: str):
    return database.all(
        """SELECT * FROM reminders
            WHERE source_module='transfer_v2' AND source_object_id=?
              AND source_event_type=?
              AND status NOT IN ('Completed','Dismissed','Cancelled')
            ORDER BY id""",
        (str(cycle_id), event_type),
    )


def _payload(
    *,
    projection,
    event_type: str,
    occurrence_key: str,
    title: str,
    description: str,
    href: str,
    now: str,
    blocker_key: str,
    branch_id: int | None,
    priority: str,
):
    dedupe_key = (
        f"transfer_v2:{projection.cycle_id}:{event_type}:"
        f"{blocker_key}:{occurrence_key}"
    )
    return {
        "dedupe_key": dedupe_key,
        "student_id": projection.student_id,
        "reminder_type": event_type,
        "title": title,
        "description": description,
        "related_module": "transfer_v2",
        "related_record_id": str(projection.cycle_id),
        "priority": priority,
        "due_date": now[:10],
        "due_datetime": now,
        "reminder_datetime": now,
        "repeat_rule": "none",
        "created_at": now,
        "updated_at": now,
        "source_type": "transfer_v2",
        "source_record_id": dedupe_key,
        "source_module": "transfer_v2",
        "source_object_type": "application_cycle",
        "source_object_id": str(projection.cycle_id),
        "source_event_type": event_type,
        "source_event_id": occurrence_key,
        "auto_generated": True,
        "suggested_action": href,
        "notes": json.dumps(
            {
                "blocker_key": blocker_key,
                "cycle_id": projection.cycle_id,
                "branch_id": branch_id,
                "href": href,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "metadata": {
            "blocker_key": blocker_key,
            "cycle_id": projection.cycle_id,
            "branch_id": branch_id,
            "action": href,
            "occurrence_key": occurrence_key,
        },
    }


def payment_proof_occurrence_key(payment_id: int, event_id: int) -> str:
    return f"payment-{payment_id}-event-{event_id}"


def reconcile_transfer_v2_reminders(
    database,
    *,
    now: str,
    cycle_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    connection = database.connect()
    try:
        if "v2_version" not in _columns(connection, "transfer_cases"):
            return {"created": 0, "closed": 0}
        if cycle_ids is None:
            ids = tuple(
                int(row[0]) for row in connection.execute(
                    """SELECT cycle.id FROM application_cycles AS cycle
                        JOIN transfer_cases AS transfer
                          ON transfer.case_id=cycle.source_student_case_id
                        WHERE transfer.v2_version=2"""
                )
            )
        else:
            ids = tuple(dict.fromkeys(int(value) for value in cycle_ids))
        facts = []
        for cycle_id in ids:
            projection = load_transfer_v2_projection(connection, cycle_id)
            facts.append((projection, _event_anchor(connection, cycle_id)))
        has_cutover = cutover_applied_at(connection) is not None
        legacy_ids = tuple(
            projection.cycle_id for projection, _anchor in facts
            if not has_cutover or projection.is_legacy
        )
        missing_proofs = {
            int(row["application_cycle_id"]): dict(row)
            for row in connection.execute(
                """SELECT payment.application_cycle_id,payment.id AS payment_id,
                          payment.branch_id,payment.final_acceptance_id,
                          COALESCE((SELECT max(event.id) FROM transfer_payment_events AS event
                                    WHERE event.payment_id=payment.id
                                      AND event.event_type IN (
                                        'created','paid_confirmed','payment_inherited',
                                        'payment_reset','proof_invalidated'
                                      )),payment.id) AS occurrence_id
                     FROM transfer_payment_aggregates AS payment
                    WHERE payment.payment_proof_status='missing'
                      AND payment.application_cycle_id IN ({})""".format(
                          ",".join("?" for _ in legacy_ids) or "NULL"
                      ),
                legacy_ids,
            )
        } if legacy_ids else {}
        if ids and "receipt_file_path" in _columns(connection, "payments"):
            for row in connection.execute(
                """SELECT branch.application_cycle_id,payment.id AS payment_id,
                          branch.id AS branch_id,acceptance.id AS final_acceptance_id,
                          link.id AS occurrence_id
                     FROM case_record_links AS link
                     JOIN payments AS payment ON payment.id=link.record_id
                     JOIN transfer_school_applications AS branch
                       ON branch.id=link.transfer_application_id
                     LEFT JOIN cycle_final_acceptances AS acceptance
                       ON acceptance.application_cycle_id=branch.application_cycle_id
                      AND acceptance.transfer_branch_id=branch.id
                      AND acceptance.acceptance_status='active'
                    WHERE link.record_type='payment'
                      AND link.validity_status IN ('current','carried_forward')
                      AND COALESCE(payment.receipt_file_path,'')=''
                      AND branch.application_cycle_id IN ({})
                    ORDER BY link.id""".format(
                        ",".join("?" for _ in ids)
                    ),
                ids,
            ):
                missing_proofs[int(row["application_cycle_id"])] = dict(row)
    finally:
        if hasattr(database, "_release_connection"):
            database._release_connection(connection)
        else:
            connection.close()

    created = 0
    closed = 0
    for projection, anchor in facts:
        existing_blockers = _active_rows(
            database, projection.cycle_id, "transfer_v2.blocker"
        )
        blocker = BLOCKER_AREAS.get(projection.stage_key)
        desired_blocker_key = None
        if projection.cycle_status == "active" and blocker is not None:
            area, action_label = blocker
            desired_blocker_key = (
                f"transfer_v2:{projection.cycle_id}:transfer_v2.blocker:"
                f"{projection.stage_key}:{anchor}"
            )
            row = create_from_event(
                database,
                _payload(
                    projection=projection,
                    event_type="transfer_v2.blocker",
                    occurrence_key=anchor,
                    title=f"Transfer V2 · {projection.stage_label}",
                    description=projection.blocker or action_label,
                    href=_deep_link(
                        projection.student_id,
                        projection.case_id,
                        area,
                        projection.stage_key,
                    ),
                    now=now,
                    blocker_key=projection.stage_key,
                    branch_id=projection.current_branch_id,
                    priority="High",
                ),
            )
            created += int(bool(row.get("_created")))
        closed += _close_reminders(
            database,
            [row for row in existing_blockers if row["dedupe_key"] != desired_blocker_key],
            now=now,
            reason=(
                f"canonical blocker changed to {projection.stage_key}"
                if desired_blocker_key else "canonical Transfer blocker satisfied"
            ),
        )

        existing_proofs = _active_rows(
            database, projection.cycle_id, "transfer_v2.payment_proof"
        )
        proof = missing_proofs.get(projection.cycle_id)
        desired_proof_key = None
        if proof is not None:
            occurrence = payment_proof_occurrence_key(
                proof["payment_id"], proof["occurrence_id"]
            )
            desired_proof_key = (
                f"transfer_v2:{projection.cycle_id}:transfer_v2.payment_proof:"
                f"payment_proof_missing:{occurrence}"
            )
            row = create_from_event(
                database,
                _payload(
                    projection=projection,
                    event_type="transfer_v2.payment_proof",
                    occurrence_key=occurrence,
                    title="Transfer V2 · 付款凭证待补",
                    description="付款已确认；付款凭证可后补且不阻塞 COE 或完成。",
                    href=_deep_link(
                        projection.student_id,
                        projection.case_id,
                        "payments",
                        "payment_proof_missing",
                    ),
                    now=now,
                    blocker_key="payment_proof_missing",
                    branch_id=int(proof["branch_id"]),
                    priority="Normal",
                ),
            )
            created += int(bool(row.get("_created")))
        closed += _close_reminders(
            database,
            [row for row in existing_proofs if row["dedupe_key"] != desired_proof_key],
            now=now,
            reason=(
                "payment proof occurrence changed"
                if desired_proof_key else "payment proof provided or invalidated"
            ),
        )
    return {"created": created, "closed": closed}
