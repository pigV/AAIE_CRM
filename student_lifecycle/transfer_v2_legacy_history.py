from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class LegacyTransferHistory:
    target_events: Sequence[Mapping[str, object]]
    offers: Sequence[Mapping[str, object]]
    offer_state_events: Sequence[Mapping[str, object]]
    payment_aggregates: Sequence[Mapping[str, object]]
    payment_events: Sequence[Mapping[str, object]]
    coe_records: Sequence[Mapping[str, object]]
    origin_events: Sequence[Mapping[str, object]]
    lifecycle_events: Sequence[Mapping[str, object]]
    completion_events: Sequence[Mapping[str, object]]


def _select(connection, sql: str, cycle_id: int):
    return tuple(dict(row) for row in connection.execute(sql, (int(cycle_id),)).fetchall())


def load_legacy_transfer_history(connection, cycle_id: int) -> LegacyTransferHistory:
    """Read existing pre-cutover Transfer facts without deriving or writing facts."""
    target_events = _select(
        connection,
        """SELECT event.id,event.branch_id,branch.school_name_snapshot AS school_name,
                  event.previous_state,event.new_state,event.event_type,
                  event.missing_checklist_json,event.note,
                  COALESCE(actor.username,'系统') AS actor_name,event.occurred_at
             FROM transfer_target_state_events AS event
             LEFT JOIN transfer_school_applications AS branch ON branch.id=event.branch_id
             LEFT JOIN users AS actor ON actor.id=event.actor_user_id
            WHERE event.application_cycle_id=?
            ORDER BY event.occurred_at,event.id""",
        cycle_id,
    )
    offers = _select(
        connection,
        """SELECT event.id,event.transfer_branch_id AS branch_id,
                  COALESCE(branch.school_name_snapshot,event.school_name_snapshot,
                           legacy.school_name,'学校待确认') AS school_name,
                  event.legacy_offer_id,legacy.offer_date,legacy.offer_status,
                  event.received_at,event.created_at,
                  COALESCE(event.recorded_by_name,actor.username,'系统') AS actor_name,
                  (SELECT state.state
                     FROM offer_received_event_state_events AS state
                    WHERE state.offer_received_event_id=event.id
                    ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1) AS event_state
             FROM offer_received_events AS event
             LEFT JOIN transfer_school_applications AS branch
               ON branch.id=event.transfer_branch_id
             LEFT JOIN offers AS legacy ON legacy.id=event.legacy_offer_id
             LEFT JOIN users AS actor ON actor.id=event.recorded_by_user_id
            WHERE event.application_cycle_id=?
            ORDER BY event.received_at,event.id""",
        cycle_id,
    )
    offer_state_events = _select(
        connection,
        """SELECT state.id,state.offer_received_event_id AS offer_id,state.state,
                  state.reason,
                  COALESCE(state.actor_name,actor.username,'系统') AS actor_name,
                  state.occurred_at,
                  COALESCE(branch.school_name_snapshot,offer.school_name_snapshot,
                           legacy.school_name,'学校待确认') AS school_name
             FROM offer_received_event_state_events AS state
             JOIN offer_received_events AS offer ON offer.id=state.offer_received_event_id
             LEFT JOIN transfer_school_applications AS branch
               ON branch.id=offer.transfer_branch_id
             LEFT JOIN offers AS legacy ON legacy.id=offer.legacy_offer_id
             LEFT JOIN users AS actor ON actor.id=state.actor_user_id
            WHERE offer.application_cycle_id=?
            ORDER BY state.occurred_at,state.id""",
        cycle_id,
    )
    payment_aggregates = _select(
        connection,
        """SELECT payment.id,payment.branch_id,
                  COALESCE(branch.school_name_snapshot,'学校待确认') AS school_name,
                  payment.paid_total,payment.last_payment_date,payment.paid_status,
                  payment.payment_proof_status,payment.inherited_from_payment_id,
                  COALESCE(actor.username,'系统') AS actor_name,
                  payment.created_at,payment.updated_at
             FROM transfer_payment_aggregates AS payment
             LEFT JOIN transfer_school_applications AS branch ON branch.id=payment.branch_id
             LEFT JOIN users AS actor ON actor.id=payment.updated_by
            WHERE payment.application_cycle_id=?
            ORDER BY payment.updated_at,payment.id""",
        cycle_id,
    )
    payment_events = _select(
        connection,
        """SELECT event.id,event.payment_id,event.event_type,event.before_json,
                  event.after_json,event.reason,
                  COALESCE(actor.username,'系统') AS actor_name,event.occurred_at,
                  COALESCE(branch.school_name_snapshot,'学校待确认') AS school_name
             FROM transfer_payment_events AS event
             JOIN transfer_payment_aggregates AS payment ON payment.id=event.payment_id
             LEFT JOIN transfer_school_applications AS branch ON branch.id=payment.branch_id
             LEFT JOIN users AS actor ON actor.id=event.actor_user_id
            WHERE payment.application_cycle_id=?
            ORDER BY event.occurred_at,event.id""",
        cycle_id,
    )
    coe_records = _select(
        connection,
        """SELECT confirmation.id,confirmation.branch_id,
                  COALESCE(branch.school_name_snapshot,coe.school_name,'学校待确认') AS school_name,
                  confirmation.coe_oshc_id,confirmation.document_id,
                  confirmation.issued_date,confirmation.status,
                  COALESCE(actor.username,'系统') AS actor_name,
                  confirmation.confirmed_at,confirmation.created_at,
                  COALESCE(document.document_type,coe.document_kind,'COE') AS record_type
             FROM transfer_coe_confirmations AS confirmation
             LEFT JOIN transfer_school_applications AS branch
               ON branch.id=confirmation.branch_id
             LEFT JOIN coe_oshc AS coe ON coe.id=confirmation.coe_oshc_id
             LEFT JOIN student_documents AS document ON document.id=confirmation.document_id
             LEFT JOIN users AS actor ON actor.id=confirmation.confirmed_by
            WHERE confirmation.application_cycle_id=?
            ORDER BY confirmation.confirmed_at,confirmation.id""",
        cycle_id,
    )
    origin_events = _select(
        connection,
        """SELECT event.id,event.previous_source_label,event.new_source_label,event.note,
                  COALESCE(actor.username,'系统') AS actor_name,event.occurred_at
             FROM transfer_v2_origin_events AS event
             LEFT JOIN users AS actor ON actor.id=event.actor_user_id
            WHERE event.application_cycle_id=?
            ORDER BY event.occurred_at,event.id""",
        cycle_id,
    )
    lifecycle_events = _select(
        connection,
        """SELECT event.id,event.branch_id,event.event_type,event.note,
                  COALESCE(actor.username,'系统') AS actor_name,event.occurred_at,
                  COALESCE(branch.school_name_snapshot,'') AS school_name
             FROM transfer_v2_lifecycle_events AS event
             LEFT JOIN transfer_school_applications AS branch ON branch.id=event.branch_id
             LEFT JOIN users AS actor ON actor.id=event.actor_user_id
            WHERE event.application_cycle_id=?
            ORDER BY event.occurred_at,event.id""",
        cycle_id,
    )
    completion_events = _select(
        connection,
        """SELECT event.id,event.final_branch_id AS branch_id,
                  event.final_school_name_snapshot AS school_name,
                  COALESCE(actor.username,'系统') AS actor_name,event.completed_at
             FROM transfer_completion_events AS event
             LEFT JOIN users AS actor ON actor.id=event.completed_by
            WHERE event.application_cycle_id=?
            ORDER BY event.completed_at,event.id""",
        cycle_id,
    )
    return LegacyTransferHistory(
        target_events=target_events,
        offers=offers,
        offer_state_events=offer_state_events,
        payment_aggregates=payment_aggregates,
        payment_events=payment_events,
        coe_records=coe_records,
        origin_events=origin_events,
        lifecycle_events=lifecycle_events,
        completion_events=completion_events,
    )
