"""Transactional school-application submission and outcome operations."""

from __future__ import annotations

from dataclasses import dataclass

from school_plan_lifecycle import SubmissionState, set_school_submission


RESULT_STATUSES = {
    "pending", "offer_received", "rejected", "no_place_or_closed",
    "withdrawn", "voided",
}
NON_OFFER_TERMINAL_RESULTS = {
    "rejected", "no_place_or_closed", "withdrawn", "voided",
}


class SchoolApplicationTrackingError(ValueError):
    pass


@dataclass(frozen=True)
class SchoolResultState:
    confirmed_school_id: int
    student_id: str
    report_id: int
    result_status: str
    result_at: str | None
    result_by_user_id: int | None
    result_by_name: str | None
    result_note: str | None
    result_offer_id: int | None
    unchanged: bool = False


def _actor(actor):
    def value(key, default=None):
        try:
            found = actor[key]
        except (KeyError, TypeError, IndexError):
            found = getattr(actor, key, default)
        return default if found is None else found

    raw_id = value("id")
    try:
        actor_id = int(raw_id) if int(raw_id) > 0 else None
    except (TypeError, ValueError):
        actor_id = None
    return actor_id, str(value("display_name", "System") or "System").strip()


def _canonical_accepted_offer_id(connection, candidates):
    """Resolve only the Offer selected by canonical active Final Acceptance."""

    candidate_ids = tuple(int(candidate.id) for candidate in candidates)
    if not candidate_ids:
        return None
    event_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(offer_received_events)")
    }
    acceptance_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(cycle_final_acceptances)")
    }
    if (
        "legacy_offer_id" not in event_columns
        or "offer_received_event_id" not in acceptance_columns
    ):
        return None
    from canonical_acceptance import active_acceptance_predicate

    predicate = active_acceptance_predicate(connection, alias="acceptance")
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = connection.execute(
        f"""SELECT event.legacy_offer_id
               FROM cycle_final_acceptances AS acceptance
               JOIN offer_received_events AS event
                 ON event.id=acceptance.offer_received_event_id
              WHERE acceptance.application_cycle_id=?
                AND {predicate}
                AND event.legacy_offer_id IN ({placeholders})
              ORDER BY acceptance.id DESC""",
        (int(candidates[0].application_cycle_id), *candidate_ids),
    ).fetchall()
    accepted = {int(row[0]) for row in rows if row[0] is not None}
    return next(iter(accepted)) if len(accepted) == 1 else None


def mark_public_group_submitted(connection, report_id, actor, submitted_at):
    rows = connection.execute(
        """SELECT id FROM school_plan_confirmed_schools
             WHERE report_id=? AND plan_sector='public' AND is_selected=1
             ORDER BY id""",
        (int(report_id),),
    ).fetchall()
    if not rows:
        raise SchoolApplicationTrackingError("当前方案没有可递交的公校。")
    connection.execute("SAVEPOINT public_school_group_submission")
    try:
        states = tuple(
            set_school_submission(connection, int(row["id"]), True, actor, submitted_at)
            for row in rows
        )
        connection.execute("RELEASE SAVEPOINT public_school_group_submission")
        return states
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT public_school_group_submission")
        connection.execute("RELEASE SAVEPOINT public_school_group_submission")
        raise


def mark_private_school_submitted(connection, confirmed_school_id, actor, submitted_at):
    row = connection.execute(
        "SELECT plan_sector,is_selected FROM school_plan_confirmed_schools WHERE id=?",
        (int(confirmed_school_id),),
    ).fetchone()
    if row is None:
        raise SchoolApplicationTrackingError("学校记录不存在。")
    if row["plan_sector"] != "private" or int(row["is_selected"] or 0) != 1:
        raise SchoolApplicationTrackingError("只有已确认的私校可以逐校递交。")
    return set_school_submission(
        connection, int(confirmed_school_id), True, actor, submitted_at
    )


def set_confirmed_school_result(
    connection, confirmed_school_id, result_status, actor, result_at,
    note="", offer_id=None,
):
    status = str(result_status or "").strip()
    if status not in RESULT_STATUSES:
        raise SchoolApplicationTrackingError("学校结果无效。")
    row = connection.execute(
        "SELECT * FROM school_plan_confirmed_schools WHERE id=?",
        (int(confirmed_school_id),),
    ).fetchone()
    if row is None:
        raise SchoolApplicationTrackingError("学校记录不存在。")
    if status != "pending" and row["submission_status"] != "submitted":
        raise SchoolApplicationTrackingError("学校申请尚未递交，不能记录结果。")

    clean_note = str(note or "").strip()
    if status in NON_OFFER_TERMINAL_RESULTS and not clean_note:
        raise SchoolApplicationTrackingError("非 Offer 结果必须填写备注。")
    resolved_offer_id = int(offer_id) if offer_id not in (None, "") else None
    if status == "offer_received":
        workflow_v2 = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_cycles'"
        ).fetchone()
        if workflow_v2:
            from application_cycle import list_matching_offers_for_confirmed_school

            candidates = list_matching_offers_for_confirmed_school(
                connection, int(confirmed_school_id)
            )
            candidate_ids = {candidate.id for candidate in candidates}
            if resolved_offer_id is None:
                canonical_offer_id = _canonical_accepted_offer_id(
                    connection, candidates
                )
                if canonical_offer_id is not None:
                    resolved_offer_id = canonical_offer_id
                elif len(candidates) == 1:
                    resolved_offer_id = candidates[0].id
                elif len(candidates) > 1:
                    raise SchoolApplicationTrackingError("存在多份符合条件的正式 Offer，请选择正确记录。")
                else:
                    raise SchoolApplicationTrackingError("收到 Offer 必须关联正式 Offer。")
            elif resolved_offer_id not in candidate_ids:
                raise SchoolApplicationTrackingError(
                    "所选 Offer 必须属于同一学生、同一申请周期及同一 CRM 学校。"
                )
        else:
            if resolved_offer_id is None:
                raise SchoolApplicationTrackingError("收到 Offer 必须关联正式 Offer。")
            offer = connection.execute(
                "SELECT student_id FROM offers WHERE id=?", (resolved_offer_id,)
            ).fetchone()
            if offer is None or str(offer["student_id"]) != str(row["student_id"]):
                raise SchoolApplicationTrackingError("Offer 不属于当前学生。")
    elif resolved_offer_id is not None:
        raise SchoolApplicationTrackingError("非 Offer 结果不能关联 Offer。")

    actor_id, actor_name = _actor(actor)
    same_result = (
        str(row["result_status"] or "") == status
        and str(row["result_note"] or "") == clean_note
        and (
            int(row["result_offer_id"])
            if row["result_offer_id"] is not None
            else None
        ) == resolved_offer_id
    )
    canonical_occurred_at = (
        str(row["result_at"])
        if same_result and row["result_at"] is not None
        else result_at
    )
    if status == "pending":
        values = (status, None, None, None, None, None, result_at, int(confirmed_school_id))
    elif same_result:
        values = None
    else:
        values = (
            status, result_at, actor_id, actor_name, clean_note or None,
            resolved_offer_id, result_at, int(confirmed_school_id),
        )
    connection.execute("SAVEPOINT school_application_result")
    try:
        if values is not None:
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                      SET result_status=?,result_at=?,result_by_user_id=?,result_by_name=?,
                          result_note=?,result_offer_id=?,updated_at=?
                    WHERE id=?""",
                values,
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_cycles'"
        ).fetchone():
            from application_cycle import synchronize_school_application_result

            synchronize_school_application_result(
                connection,
                int(confirmed_school_id),
                result_status=status,
                occurred_at=canonical_occurred_at,
                actor_user_id=actor_id,
                actor_name=actor_name,
                result_note=clean_note or None,
                legacy_offer_id=resolved_offer_id,
            )
        connection.execute("RELEASE SAVEPOINT school_application_result")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT school_application_result")
        connection.execute("RELEASE SAVEPOINT school_application_result")
        raise
    updated = connection.execute(
        "SELECT * FROM school_plan_confirmed_schools WHERE id=?",
        (int(confirmed_school_id),),
    ).fetchone()
    return SchoolResultState(
        confirmed_school_id=int(updated["id"]), student_id=str(updated["student_id"]),
        report_id=int(updated["report_id"]), result_status=str(updated["result_status"]),
        result_at=updated["result_at"], result_by_user_id=updated["result_by_user_id"],
        result_by_name=updated["result_by_name"], result_note=updated["result_note"],
        result_offer_id=updated["result_offer_id"],
        unchanged=same_result,
    )
