from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from workflow_offer_events import (
    WorkflowEventConflictError,
    change_final_acceptance,
    select_final_acceptance,
)
from student_lifecycle.transfer_v2_cutover import (
    cutover_applied_at,
    is_new_transfer_cycle,
)


class TransferV2AdmissionsError(RuntimeError):
    pass


class TransferV2AdmissionsValidationError(TransferV2AdmissionsError):
    pass


@dataclass(frozen=True)
class TransferV2OfferState:
    event_id: int
    offer_event_id: int
    state: str
    occurred_at: str


@dataclass(frozen=True)
class TransferV2Acceptance:
    acceptance_id: int
    cycle_id: int
    branch_id: int
    offer_event_id: int
    status: str


@dataclass(frozen=True)
class TransferV2Payment:
    payment_id: int
    acceptance_id: int
    branch_id: int
    offer_event_id: int
    paid_total: float | None
    last_payment_date: str | None
    paid_status: str
    payment_proof_status: str
    inherited_from_payment_id: int | None


@dataclass(frozen=True)
class TransferV2CoeConfirmation:
    confirmation_id: int
    acceptance_id: int
    branch_id: int
    coe_oshc_id: int
    document_id: int | None
    issued_date: str
    confirmed_by: int
    confirmed_at: str


def _default_uid(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


class TransferV2AdmissionsService:
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
            raise TransferV2AdmissionsValidationError(f"{label} is required")
        return normalized

    @staticmethod
    def _actor(connection: sqlite3.Connection, actor_user_id: int) -> str:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info('users')")
        }
        name_column = "display_name" if "display_name" in columns else "name"
        row = connection.execute(
            f'SELECT COALESCE("{name_column}",\'\') FROM users WHERE id=?',
            (int(actor_user_id),),
        ).fetchone()
        if row is None:
            raise TransferV2AdmissionsValidationError("actor not found")
        return str(row[0] or "")

    @staticmethod
    def _branch(
        connection: sqlite3.Connection, cycle_id: int, branch_id: int
    ) -> sqlite3.Row:
        has_removal = any(
            str(column[1]) == "removed_at"
            for column in connection.execute(
                "PRAGMA table_info(transfer_school_applications)"
            )
        )
        active_clause = " AND branch.removed_at IS NULL" if has_removal else ""
        row = connection.execute(
            f"""SELECT branch.*,cycle.student_id,transfer_case.v2_version
                 FROM transfer_school_applications AS branch
                 JOIN application_cycles AS cycle
                   ON cycle.id=branch.application_cycle_id
                 JOIN transfer_cases AS transfer_case
                   ON transfer_case.case_id=branch.case_id
                WHERE branch.id=? AND branch.application_cycle_id=?
                  AND cycle.cycle_type='transfer' AND cycle.status='active'{active_clause}""",
            (int(branch_id), int(cycle_id)),
        ).fetchone()
        if row is None or int(row["v2_version"] or 0) != 2:
            raise TransferV2AdmissionsValidationError("Transfer V2 branch not found")
        if cutover_applied_at(connection) is not None and not is_new_transfer_cycle(
            connection, int(cycle_id)
        ):
            raise TransferV2AdmissionsValidationError("Legacy Transfer is read only")
        return row

    @staticmethod
    def _offer(
        connection: sqlite3.Connection,
        cycle_id: int,
        branch_id: int,
        offer_event_id: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM offer_received_events
                WHERE id=? AND application_cycle_id=? AND transfer_branch_id=?""",
            (int(offer_event_id), int(cycle_id), int(branch_id)),
        ).fetchone()
        if row is None:
            raise TransferV2AdmissionsValidationError(
                "Offer does not belong to the exact Cycle and Branch"
            )
        return row

    @staticmethod
    def _latest_offer_state(
        connection: sqlite3.Connection, offer_event_id: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM offer_received_event_state_events
                WHERE offer_received_event_id=?
                ORDER BY occurred_at DESC,id DESC LIMIT 1""",
            (int(offer_event_id),),
        ).fetchone()

    @staticmethod
    def _sync_legacy_offer_status(
        connection: sqlite3.Connection, offer_event_id: int, status: str
    ) -> None:
        if "offer_status" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info('offers')")
        }:
            return
        connection.execute(
            """UPDATE offers SET offer_status=? WHERE id=(
                 SELECT legacy_offer_id FROM offer_received_events WHERE id=?
               )""",
            (str(status), int(offer_event_id)),
        )

    def _event_uid(self, command: str, key: str) -> str:
        return self.uid_factory(f"transfer-v2-{command}-{key}")

    @staticmethod
    def _request_json(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _replay_receipt(
        connection: sqlite3.Connection,
        event_uid: str,
        command_type: str,
        payload_json: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM transfer_v2_command_receipts WHERE event_uid=?",
            (event_uid,),
        ).fetchone()
        if row is None:
            return None
        if row["command_type"] != command_type or row["request_json"] != payload_json:
            raise TransferV2AdmissionsValidationError(
                "idempotency key conflicts with an earlier command"
            )
        return row

    @staticmethod
    def _record_receipt(
        connection: sqlite3.Connection,
        *,
        event_uid: str,
        cycle_id: int,
        command_type: str,
        aggregate_type: str,
        aggregate_id: int,
        payload_json: str,
        actor_user_id: int,
        timestamp: str,
    ) -> None:
        connection.execute(
            """INSERT INTO transfer_v2_command_receipts(
                 event_uid,application_cycle_id,command_type,aggregate_type,
                 aggregate_id,request_json,actor_user_id,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event_uid,
                int(cycle_id),
                command_type,
                aggregate_type,
                int(aggregate_id),
                payload_json,
                int(actor_user_id),
                timestamp,
                timestamp,
            ),
        )

    @staticmethod
    def _offer_state_from_row(row: sqlite3.Row) -> TransferV2OfferState:
        return TransferV2OfferState(
            event_id=int(row["id"]),
            offer_event_id=int(row["offer_received_event_id"]),
            state=str(row["state"]),
            occurred_at=str(row["occurred_at"]),
        )

    @staticmethod
    def _acceptance_from_row(row: sqlite3.Row) -> TransferV2Acceptance:
        return TransferV2Acceptance(
            acceptance_id=int(row["id"]),
            cycle_id=int(row["application_cycle_id"]),
            branch_id=int(row["transfer_branch_id"]),
            offer_event_id=int(row["offer_received_event_id"]),
            status=str(row["acceptance_status"]),
        )

    @staticmethod
    def _payment_from_row(row: sqlite3.Row) -> TransferV2Payment:
        return TransferV2Payment(
            payment_id=int(row["id"]),
            acceptance_id=int(row["final_acceptance_id"]),
            branch_id=int(row["branch_id"]),
            offer_event_id=int(row["accepted_offer_event_id"]),
            paid_total=float(row["paid_total"]) if row["paid_total"] is not None else None,
            last_payment_date=(
                str(row["last_payment_date"]) if row["last_payment_date"] else None
            ),
            paid_status=str(row["paid_status"]),
            payment_proof_status=str(row["payment_proof_status"]),
            inherited_from_payment_id=(
                int(row["inherited_from_payment_id"])
                if row["inherited_from_payment_id"] is not None
                else None
            ),
        )

    @staticmethod
    def _coe_from_row(row: sqlite3.Row) -> TransferV2CoeConfirmation:
        return TransferV2CoeConfirmation(
            confirmation_id=int(row["id"]),
            acceptance_id=int(row["final_acceptance_id"]),
            branch_id=int(row["branch_id"]),
            coe_oshc_id=int(row["coe_oshc_id"]),
            document_id=int(row["document_id"]) if row["document_id"] is not None else None,
            issued_date=str(row["issued_date"]),
            confirmed_by=int(row["confirmed_by"]),
            confirmed_at=str(row["confirmed_at"]),
        )

    @staticmethod
    def _active_acceptance(
        connection: sqlite3.Connection, cycle_id: int, acceptance_id: int | None = None
    ) -> sqlite3.Row:
        sql = """SELECT * FROM cycle_final_acceptances
                  WHERE application_cycle_id=? AND acceptance_status='active'"""
        parameters: tuple[int, ...] = (int(cycle_id),)
        if acceptance_id is not None:
            sql += " AND id=?"
            parameters += (int(acceptance_id),)
        row = connection.execute(sql, parameters).fetchone()
        if row is None or row["transfer_branch_id"] is None:
            raise TransferV2AdmissionsValidationError("current acceptance not found")
        return row

    def offer_is_valid(self, cycle_id: int, branch_id: int, offer_event_id: int) -> bool:
        with closing(self._connect()) as connection:
            self._branch(connection, cycle_id, branch_id)
            self._offer(connection, cycle_id, branch_id, offer_event_id)
            state = self._latest_offer_state(connection, offer_event_id)
            return state is not None and state["state"] == "ACTIVE"

    def _change_offer_state(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        offer_event_id: int,
        state: str,
        actor_user_id: int,
        reason: str | None,
        idempotency_key: str,
        command_type: str,
    ) -> TransferV2OfferState:
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._event_uid(command_type, key)
        timestamp = self.clock()
        payload_json = self._request_json({
            "cycle_id": int(cycle_id),
            "branch_id": int(branch_id),
            "offer_event_id": int(offer_event_id),
            "state": state,
            "reason": reason,
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._actor(connection, actor_user_id)
            self._branch(connection, cycle_id, branch_id)
            self._offer(connection, cycle_id, branch_id, offer_event_id)
            receipt = self._replay_receipt(
                connection, event_uid, command_type, payload_json
            )
            if receipt is not None:
                row = connection.execute(
                    "SELECT * FROM offer_received_event_state_events WHERE id=?",
                    (int(receipt["aggregate_id"]),),
                ).fetchone()
                connection.commit()
                return self._offer_state_from_row(row)
            cursor = connection.execute(
                """INSERT INTO offer_received_event_state_events(
                     offer_received_event_id,state,reason,occurred_at,actor_user_id,
                     actor_name,idempotency_key,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    int(offer_event_id),
                    state,
                    reason,
                    timestamp,
                    int(actor_user_id),
                    self._actor(connection, actor_user_id),
                    event_uid,
                    timestamp,
                ),
            )
            state_event_id = int(cursor.lastrowid)
            new_cycle = is_new_transfer_cycle(connection, int(cycle_id))
            if not new_cycle:
                self._sync_legacy_offer_status(
                    connection,offer_event_id,"received" if state == "ACTIVE" else "cancelled"
                )
            if state == "ACTIVE":
                self._append_branch_state_if_forward(
                    connection,
                    cycle_id=cycle_id,
                    branch_id=branch_id,
                    new_state="offer",
                    event_type="offer_confirmed" if new_cycle else "offer_valid_confirmed",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    event_uid=f"{event_uid}:branch",
                    note=reason,
                )
            self._record_receipt(
                connection,
                event_uid=event_uid,
                cycle_id=cycle_id,
                command_type=command_type,
                aggregate_type="offer_state_event",
                aggregate_id=state_event_id,
                payload_json=payload_json,
                actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM offer_received_event_state_events WHERE id=?",
                (state_event_id,),
            ).fetchone()
            connection.commit()
            return self._offer_state_from_row(row)

    def confirm_offer_valid(self, **kwargs) -> TransferV2OfferState:
        return self._change_offer_state(
            **kwargs, state="ACTIVE", reason=None, command_type="confirm_offer_valid"
        )

    def invalidate_offer(self, **kwargs) -> TransferV2OfferState:
        reason = self._required(kwargs.pop("reason", None), "reason")
        return self._change_offer_state(
            **kwargs,
            state="VOID",
            reason=reason,
            command_type="invalidate_offer",
        )

    @staticmethod
    def _append_branch_state_if_forward(
        connection: sqlite3.Connection,
        *,
        cycle_id: int,
        branch_id: int,
        new_state: str,
        event_type: str,
        actor_user_id: int,
        timestamp: str,
        event_uid: str,
        note: str | None,
    ) -> None:
        current = connection.execute(
            """SELECT new_state FROM transfer_target_state_events
                WHERE branch_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1""",
            (int(branch_id),),
        ).fetchone()
        previous = str(current[0]) if current is not None else None
        if is_new_transfer_cycle(connection, int(cycle_id)):
            if new_state != "offer":
                return
            event_type = "offer_confirmed"
            if previous == "offer":
                return
            connection.execute(
                """INSERT INTO transfer_target_state_events(
                     event_uid,application_cycle_id,branch_id,previous_state,new_state,
                     event_type,missing_checklist_json,note,actor_user_id,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,NULL,?,?,?,?)""",
                (
                    event_uid,int(cycle_id),int(branch_id),previous,"offer",event_type,
                    note,int(actor_user_id),timestamp,timestamp,
                ),
            )
            return
        rank = {
            "materials_preparation": 0,
            "application_in_progress": 1,
            "offer": 2,
            "accepted": 3,
            "paid": 4,
            "coe": 5,
            "completed": 6,
        }
        if previous == new_state or rank.get(previous, -1) > rank.get(new_state, -1):
            return
        connection.execute(
            """INSERT INTO transfer_target_state_events(
                 event_uid,application_cycle_id,branch_id,previous_state,new_state,
                 event_type,missing_checklist_json,note,actor_user_id,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,NULL,?,?,?,?)""",
            (
                event_uid,
                int(cycle_id),
                int(branch_id),
                previous,
                new_state,
                event_type,
                note,
                int(actor_user_id),
                timestamp,
                timestamp,
            ),
        )

    def _valid_offer_rows(
        self, connection: sqlite3.Connection, cycle_id: int, branch_id: int
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT offer.* FROM offer_received_events AS offer
                WHERE offer.application_cycle_id=? AND offer.transfer_branch_id=?
                  AND COALESCE((
                    SELECT state.state FROM offer_received_event_state_events AS state
                     WHERE state.offer_received_event_id=offer.id
                     ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
                  ),'UNCONFIRMED')='ACTIVE'
                ORDER BY offer.received_at DESC,offer.id DESC""",
            (int(cycle_id), int(branch_id)),
        ).fetchall()

    def accept_offer(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        offer_event_id: int,
        actor_user_id: int,
        confirm_non_latest: bool,
        idempotency_key: str,
    ) -> TransferV2Acceptance:
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._event_uid("accept-offer", key)
        timestamp = self.clock()
        payload_json = self._request_json({
            "cycle_id": int(cycle_id),
            "branch_id": int(branch_id),
            "offer_event_id": int(offer_event_id),
            "confirm_non_latest": bool(confirm_non_latest),
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor_name = self._actor(connection, actor_user_id)
            self._branch(connection, cycle_id, branch_id)
            self._offer(connection, cycle_id, branch_id, offer_event_id)
            receipt = self._replay_receipt(connection, event_uid, "accept_offer", payload_json)
            if receipt is not None:
                row = connection.execute(
                    "SELECT * FROM cycle_final_acceptances WHERE id=?",
                    (int(receipt["aggregate_id"]),),
                ).fetchone()
                connection.commit()
                return self._acceptance_from_row(row)
            valid = self._valid_offer_rows(connection, cycle_id, branch_id)
            selected = next((row for row in valid if int(row["id"]) == int(offer_event_id)), None)
            if selected is None:
                raise TransferV2AdmissionsValidationError("Offer has not been confirmed valid")
            if int(valid[0]["id"]) != int(offer_event_id) and not confirm_non_latest:
                raise TransferV2AdmissionsValidationError("你正在接受非最新 Offer")
            new_cycle = is_new_transfer_cycle(connection, int(cycle_id))
            if new_cycle:
                signed_offer = connection.execute(
                    """SELECT 1 FROM offer_received_events AS event
                         JOIN offers AS offer ON offer.id=event.legacy_offer_id
                        WHERE event.id=? AND event.application_cycle_id=?
                          AND event.transfer_branch_id=?
                          AND offer.offer_status IN ('signed','signed_received','accepted')""",
                    (int(offer_event_id), int(cycle_id), int(branch_id)),
                ).fetchone()
                if signed_offer is None:
                    raise TransferV2AdmissionsValidationError(
                        "已签署 Offer 完成后，才能设为当前接受学校"
                    )
            existing = connection.execute(
                """SELECT * FROM cycle_final_acceptances
                    WHERE application_cycle_id=? AND acceptance_status='active'""",
                (int(cycle_id),),
            ).fetchone()
            if existing is not None and int(existing["offer_received_event_id"]) != int(offer_event_id):
                raise TransferV2AdmissionsValidationError(
                    "another current acceptance exists; use switch acceptance"
                )
            try:
                acceptance = select_final_acceptance(
                    connection,
                    int(cycle_id),
                    int(offer_event_id),
                    selected_at=timestamp,
                    actor_user_id=int(actor_user_id),
                    actor_name=actor_name,
                )
            except WorkflowEventConflictError as error:
                raise TransferV2AdmissionsValidationError(str(error)) from None
            connection.execute(
                "UPDATE cycle_final_acceptances SET transfer_branch_id=? WHERE id=?",
                (int(branch_id), int(acceptance.id)),
            )
            self._sync_legacy_offer_status(connection,offer_event_id,"accepted")
            self._append_branch_state_if_forward(
                connection,
                cycle_id=cycle_id,
                branch_id=branch_id,
                new_state="accepted",
                event_type="offer_accepted",
                actor_user_id=actor_user_id,
                timestamp=timestamp,
                event_uid=f"{event_uid}:branch",
                note=None,
            )
            self._record_receipt(
                connection,
                event_uid=event_uid,
                cycle_id=cycle_id,
                command_type="accept_offer",
                aggregate_type="final_acceptance",
                aggregate_id=int(acceptance.id),
                payload_json=payload_json,
                actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM cycle_final_acceptances WHERE id=?", (int(acceptance.id),)
            ).fetchone()
            connection.commit()
            return self._acceptance_from_row(row)

    def switch_acceptance(
        self,
        *,
        cycle_id: int,
        current_acceptance_id: int,
        new_branch_id: int,
        new_offer_event_id: int,
        old_branch_outcome: str,
        payment_action: str,
        actor_user_id: int,
        reason: str,
        idempotency_key: str,
    ) -> TransferV2Acceptance:
        if old_branch_outcome not in {"withdrawn", "other"}:
            raise TransferV2AdmissionsValidationError(
                "old branch outcome must be withdrawn or other"
            )
        if payment_action not in {"inherit", "reset"}:
            raise TransferV2AdmissionsValidationError(
                "payment action must be inherit or reset"
            )
        normalized_reason = self._required(reason, "reason")
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._event_uid("switch-acceptance", key)
        timestamp = self.clock()
        payload_json = self._request_json({
            "cycle_id": int(cycle_id),
            "current_acceptance_id": int(current_acceptance_id),
            "new_branch_id": int(new_branch_id),
            "new_offer_event_id": int(new_offer_event_id),
            "old_branch_outcome": old_branch_outcome,
            "payment_action": payment_action,
            "reason": normalized_reason,
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            actor_name = self._actor(connection, actor_user_id)
            receipt = self._replay_receipt(
                connection, event_uid, "switch_acceptance", payload_json
            )
            if receipt is not None:
                row = connection.execute(
                    "SELECT * FROM cycle_final_acceptances WHERE id=?",
                    (int(receipt["aggregate_id"]),),
                ).fetchone()
                connection.commit()
                return self._acceptance_from_row(row)
            old = self._active_acceptance(connection, cycle_id, current_acceptance_id)
            old_branch = self._branch(
                connection, cycle_id, int(old["transfer_branch_id"])
            )
            if connection.execute(
                """SELECT 1 FROM transfer_coe_confirmations
                    WHERE application_cycle_id=? AND final_acceptance_id=?
                      AND status='confirmed'""",
                (int(cycle_id), int(current_acceptance_id)),
            ).fetchone() is not None:
                raise TransferV2AdmissionsValidationError(
                    "COE confirmed; accepted school and Offer are locked"
                )
            self._branch(connection, cycle_id, new_branch_id)
            self._offer(connection, cycle_id, new_branch_id, new_offer_event_id)
            if not any(
                int(row["id"]) == int(new_offer_event_id)
                for row in self._valid_offer_rows(connection, cycle_id, new_branch_id)
            ):
                raise TransferV2AdmissionsValidationError("replacement Offer is not valid")
            new_cycle = is_new_transfer_cycle(connection, int(cycle_id))
            if new_cycle:
                if payment_action != "reset":
                    raise TransferV2AdmissionsValidationError(
                        "切换已接受学校必须重新确认新分支付款"
                    )
                eligible = connection.execute(
                    """SELECT 1
                         FROM offer_received_events AS event
                         JOIN offers AS offer ON offer.id=event.legacy_offer_id
                        WHERE event.id=? AND event.application_cycle_id=?
                          AND event.transfer_branch_id=?
                          AND offer.offer_status IN ('signed','signed_received','accepted')""",
                    (int(new_offer_event_id),int(cycle_id),int(new_branch_id)),
                ).fetchone()
                if eligible is None:
                    raise TransferV2AdmissionsValidationError(
                        "替换学校必须先完成已签署 Offer"
                    )
            try:
                changed = change_final_acceptance(
                    connection,
                    int(cycle_id),
                    int(new_offer_event_id),
                    occurred_at=timestamp,
                    actor_user_id=int(actor_user_id),
                    reason=normalized_reason,
                    actor_name=actor_name,
                )
            except (WorkflowEventConflictError, sqlite3.IntegrityError) as error:
                raise TransferV2AdmissionsValidationError(str(error)) from None
            connection.execute(
                "UPDATE cycle_final_acceptances SET transfer_branch_id=? WHERE id=?",
                (int(new_branch_id), int(changed.id)),
            )
            if new_cycle:
                self._sync_legacy_offer_status(
                    connection,int(old["offer_received_event_id"]),
                    "withdrawn" if old_branch_outcome == "withdrawn" else "cancelled",
                )
                self._sync_legacy_offer_status(connection,new_offer_event_id,"accepted")
                prior = connection.execute(
                    """SELECT new_state FROM transfer_target_state_events
                         WHERE branch_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1""",
                    (int(old["transfer_branch_id"]),),
                ).fetchone()
                previous_state = str(prior[0]) if prior is not None else None
                outcome_state = (
                    "parent_withdrawn" if old_branch_outcome == "withdrawn" else "other"
                )
                connection.execute(
                    """INSERT INTO transfer_target_state_events(
                         event_uid,application_cycle_id,branch_id,previous_state,new_state,
                         event_type,note,actor_user_id,occurred_at,created_at)
                       VALUES(?,?,?,?,?,'acceptance_switched',?,?,?,?)""",
                    (
                        f"{event_uid}:old-branch",int(cycle_id),
                        int(old["transfer_branch_id"]),previous_state,outcome_state,
                        normalized_reason,int(actor_user_id),timestamp,timestamp,
                    ),
                )
                self._append_branch_state_if_forward(
                    connection, cycle_id=cycle_id, branch_id=new_branch_id,
                    new_state="accepted", event_type="acceptance_switched",
                    actor_user_id=actor_user_id, timestamp=timestamp,
                    event_uid=f"{event_uid}:new-branch", note=normalized_reason,
                )
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_record_links'"
                ).fetchone() is not None:
                    connection.execute(
                        """UPDATE case_record_links SET validity_status='historical'
                             WHERE case_id=? AND transfer_application_id=?
                               AND record_type='payment' AND validity_status='current'""",
                        (int(old_branch["case_id"]), int(old["transfer_branch_id"])),
                    )
                self._record_receipt(
                    connection,event_uid=event_uid,cycle_id=cycle_id,
                    command_type="switch_acceptance",aggregate_type="final_acceptance",
                    aggregate_id=int(changed.id),payload_json=payload_json,
                    actor_user_id=actor_user_id,timestamp=timestamp,
                )
                row = connection.execute(
                    "SELECT * FROM cycle_final_acceptances WHERE id=?", (int(changed.id),)
                ).fetchone()
                connection.commit()
                return self._acceptance_from_row(row)
            self._sync_legacy_offer_status(
                connection,int(old["offer_received_event_id"]),
                "withdrawn" if old_branch_outcome == "withdrawn" else "cancelled",
            )
            self._sync_legacy_offer_status(
                connection,new_offer_event_id,"accepted"
            )
            if old_branch_outcome == "withdrawn":
                connection.execute(
                    """INSERT INTO transfer_target_state_events(
                         event_uid,application_cycle_id,branch_id,previous_state,new_state,
                         event_type,note,actor_user_id,occurred_at,created_at)
                       VALUES(?,?,?,?,?,'acceptance_switched_withdrawn',?,?,?,?)""",
                    (
                        f"{event_uid}:old-branch",
                        int(cycle_id),
                        int(old["transfer_branch_id"]),
                        "accepted",
                        "withdrawn",
                        normalized_reason,
                        int(actor_user_id),
                        timestamp,
                        timestamp,
                    ),
                )
            self._append_branch_state_if_forward(
                connection,
                cycle_id=cycle_id,
                branch_id=new_branch_id,
                new_state="accepted",
                event_type="acceptance_switched",
                actor_user_id=actor_user_id,
                timestamp=timestamp,
                event_uid=f"{event_uid}:new-branch",
                note=normalized_reason,
            )
            old_payment = connection.execute(
                "SELECT * FROM transfer_payment_aggregates WHERE final_acceptance_id=?",
                (int(current_acceptance_id),),
            ).fetchone()
            authoritative_payment_links = []
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_record_links'"
            ).fetchone() is not None:
                authoritative_payment_links = connection.execute(
                    """SELECT * FROM case_record_links
                         WHERE case_id=? AND transfer_application_id=?
                           AND record_type='payment'
                           AND validity_status IN ('current','carried_forward')
                         ORDER BY id""",
                    (int(old_branch["case_id"]), int(old["transfer_branch_id"])),
                ).fetchall()
            payment_id = None
            authoritative_payment_inherited = False
            if authoritative_payment_links:
                if payment_action == "inherit":
                    for link in authoritative_payment_links:
                        existing_link = connection.execute(
                            """SELECT id FROM case_record_links
                                 WHERE case_id=? AND transfer_application_id=?
                                   AND record_type='payment' AND record_id=?""",
                            (int(old_branch["case_id"]), int(new_branch_id), int(link["record_id"])),
                        ).fetchone()
                        if existing_link is None:
                            connection.execute(
                                """INSERT INTO case_record_links(
                                     case_id,transfer_application_id,record_type,record_id,
                                     document_id,link_role,validity_status,effective_from,
                                     created_by,created_at)
                                   VALUES(?,?,'payment',?,?,
                                          'carried_forward','carried_forward',?,?,?)""",
                                (
                                    int(old_branch["case_id"]), int(new_branch_id),
                                    int(link["record_id"]), link["document_id"], timestamp,
                                    int(actor_user_id), timestamp,
                                ),
                            )
                    connection.execute(
                        """UPDATE cycle_final_acceptances
                              SET payment_completed_at=COALESCE(payment_completed_at,?),
                                  updated_at=?
                            WHERE id=?""",
                        (old["payment_completed_at"] or timestamp, timestamp, int(changed.id)),
                    )
                    payment_id = int(authoritative_payment_links[0]["record_id"])
                    authoritative_payment_inherited = True
            else:
                payment_id = self._create_switched_payment(
                    connection,
                    cycle_id=cycle_id,
                    branch_id=new_branch_id,
                    acceptance_id=int(changed.id),
                    offer_event_id=new_offer_event_id,
                    old_payment=old_payment,
                    payment_action=payment_action,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    event_uid=f"{event_uid}:payment",
                    reason=normalized_reason,
                )
            self._record_receipt(
                connection,
                event_uid=event_uid,
                cycle_id=cycle_id,
                command_type="switch_acceptance",
                aggregate_type="final_acceptance",
                aggregate_id=int(changed.id),
                payload_json=payload_json,
                actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            if payment_id is not None and payment_action == "inherit" and (
                old_payment is not None or authoritative_payment_inherited
            ):
                self._append_branch_state_if_forward(
                    connection,
                    cycle_id=cycle_id,
                    branch_id=new_branch_id,
                    new_state="paid",
                    event_type="payment_inherited",
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                    event_uid=f"{event_uid}:paid-branch",
                    note=normalized_reason,
                )
            row = connection.execute(
                "SELECT * FROM cycle_final_acceptances WHERE id=?", (int(changed.id),)
            ).fetchone()
            connection.commit()
            return self._acceptance_from_row(row)

    def _create_switched_payment(
        self,
        connection: sqlite3.Connection,
        *,
        cycle_id: int,
        branch_id: int,
        acceptance_id: int,
        offer_event_id: int,
        old_payment: sqlite3.Row | None,
        payment_action: str,
        actor_user_id: int,
        timestamp: str,
        event_uid: str,
        reason: str,
    ) -> int:
        inherit = payment_action == "inherit" and old_payment is not None
        payment_uid = f"{event_uid}:aggregate"
        cursor = connection.execute(
            """INSERT INTO transfer_payment_aggregates(
                 payment_uid,application_cycle_id,branch_id,final_acceptance_id,
                 accepted_offer_event_id,paid_total,last_payment_date,paid_status,
                 payment_proof_status,inherited_from_payment_id,created_by,created_at,
                 updated_by,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payment_uid,
                int(cycle_id),
                int(branch_id),
                int(acceptance_id),
                int(offer_event_id),
                old_payment["paid_total"] if inherit else None,
                old_payment["last_payment_date"] if inherit else None,
                old_payment["paid_status"] if inherit else "unpaid",
                old_payment["payment_proof_status"] if inherit else "missing",
                int(old_payment["id"]) if inherit else None,
                int(actor_user_id),
                timestamp,
                int(actor_user_id),
                timestamp,
            ),
        )
        payment_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO transfer_payment_events(
                 event_uid,payment_id,event_type,before_json,after_json,reason,
                 actor_user_id,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event_uid,
                payment_id,
                "payment_inherited" if inherit else "payment_reset",
                self._request_json(dict(old_payment)) if old_payment is not None else None,
                self._request_json({
                    "paid_status": old_payment["paid_status"] if inherit else "unpaid",
                    "inherited_from_payment_id": int(old_payment["id"]) if inherit else None,
                }),
                reason,
                int(actor_user_id),
                timestamp,
                timestamp,
            ),
        )
        return payment_id

    def confirm_payment(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        acceptance_id: int,
        paid_total: float | None,
        last_payment_date: str,
        proof_provided: bool,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2Payment:
        if paid_total is not None and float(paid_total) < 0:
            raise TransferV2AdmissionsValidationError("paid total cannot be negative")
        payment_date = self._required(last_payment_date, "last payment date")
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._event_uid("confirm-payment", key)
        timestamp = self.clock()
        payload_json = self._request_json({
            "cycle_id": int(cycle_id),
            "branch_id": int(branch_id),
            "acceptance_id": int(acceptance_id),
            "paid_total": float(paid_total) if paid_total is not None else None,
            "last_payment_date": payment_date,
            "proof_provided": bool(proof_provided),
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._actor(connection, actor_user_id)
            if is_new_transfer_cycle(connection, int(cycle_id)):
                raise TransferV2AdmissionsValidationError(
                    "新 Transfer 付款只能在学生资料页确认正式付款记录"
                )
            acceptance = self._active_acceptance(connection, cycle_id, acceptance_id)
            if int(acceptance["transfer_branch_id"]) != int(branch_id):
                raise TransferV2AdmissionsValidationError("acceptance Branch mismatch")
            receipt = self._replay_receipt(
                connection, event_uid, "confirm_payment", payload_json
            )
            if receipt is not None:
                row = connection.execute(
                    "SELECT * FROM transfer_payment_aggregates WHERE id=?",
                    (int(receipt["aggregate_id"]),),
                ).fetchone()
                connection.commit()
                return self._payment_from_row(row)
            existing = connection.execute(
                "SELECT * FROM transfer_payment_aggregates WHERE final_acceptance_id=?",
                (int(acceptance_id),),
            ).fetchone()
            proof_status = "provided" if proof_provided else "missing"
            if existing is None:
                payment_id = int(connection.execute(
                    """INSERT INTO transfer_payment_aggregates(
                         payment_uid,application_cycle_id,branch_id,final_acceptance_id,
                         accepted_offer_event_id,paid_total,last_payment_date,paid_status,
                         payment_proof_status,created_by,created_at,updated_by,updated_at)
                       VALUES(?,?,?,?,?,?,?,'paid',?,?,?,?,?)""",
                    (
                        f"{event_uid}:aggregate",
                        int(cycle_id),
                        int(branch_id),
                        int(acceptance_id),
                        int(acceptance["offer_received_event_id"]),
                        float(paid_total) if paid_total is not None else None,
                        payment_date,
                        proof_status,
                        int(actor_user_id),
                        timestamp,
                        int(actor_user_id),
                        timestamp,
                    ),
                ).lastrowid)
                before_json = None
            else:
                payment_id = int(existing["id"])
                before_json = self._request_json(dict(existing))
                connection.execute(
                    """UPDATE transfer_payment_aggregates
                       SET paid_total=?,last_payment_date=?,paid_status='paid',
                           payment_proof_status=?,updated_by=?,updated_at=?
                       WHERE id=?""",
                    (
                        float(paid_total) if paid_total is not None else None,
                        payment_date,
                        proof_status,
                        int(actor_user_id),
                        timestamp,
                        payment_id,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM transfer_payment_aggregates WHERE id=?", (payment_id,)
            ).fetchone()
            connection.execute(
                """INSERT INTO transfer_payment_events(
                     event_uid,payment_id,event_type,before_json,after_json,
                     actor_user_id,occurred_at,created_at)
                   VALUES(?,?,?, ?,?,?,?,?)""",
                (
                    f"{event_uid}:event",
                    payment_id,
                    "paid_confirmed" if existing is None else "payment_updated",
                    before_json,
                    self._request_json(dict(row)),
                    int(actor_user_id),
                    timestamp,
                    timestamp,
                ),
            )
            self._append_branch_state_if_forward(
                connection,
                cycle_id=cycle_id,
                branch_id=branch_id,
                new_state="paid",
                event_type="payment_confirmed",
                actor_user_id=actor_user_id,
                timestamp=timestamp,
                event_uid=f"{event_uid}:branch",
                note=None,
            )
            self._record_receipt(
                connection,
                event_uid=event_uid,
                cycle_id=cycle_id,
                command_type="confirm_payment",
                aggregate_type="payment",
                aggregate_id=payment_id,
                payload_json=payload_json,
                actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            connection.commit()
            return self._payment_from_row(row)

    def current_payment(
        self, cycle_id: int, acceptance_id: int
    ) -> TransferV2Payment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM transfer_payment_aggregates
                    WHERE application_cycle_id=? AND final_acceptance_id=?""",
                (int(cycle_id), int(acceptance_id)),
            ).fetchone()
            return self._payment_from_row(row) if row is not None else None

    def start_waiting_coe(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        acceptance_id: int,
        actor_user_id: int,
        idempotency_key: str,
    ) -> int:
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._event_uid("waiting-coe", key)
        timestamp = self.clock()
        payload_json = self._request_json({
            "cycle_id": int(cycle_id),
            "branch_id": int(branch_id),
            "acceptance_id": int(acceptance_id),
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._actor(connection, actor_user_id)
            if is_new_transfer_cycle(connection, int(cycle_id)):
                raise TransferV2AdmissionsValidationError(
                    "新 Transfer 在已接受并付款后直接进入更新 COE"
                )
            acceptance = self._active_acceptance(connection, cycle_id, acceptance_id)
            if int(acceptance["transfer_branch_id"]) != int(branch_id):
                raise TransferV2AdmissionsValidationError("acceptance Branch mismatch")
            authoritative_payment = None
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_record_links'"
            ).fetchone() is not None and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payments'"
            ).fetchone() is not None:
                authoritative_payment = connection.execute(
                    """SELECT 1
                         FROM case_record_links AS link
                         JOIN payments AS payment ON payment.id=link.record_id
                         JOIN transfer_school_applications AS branch
                           ON branch.id=link.transfer_application_id
                        WHERE branch.application_cycle_id=? AND branch.id=?
                          AND link.record_type='payment'
                          AND link.validity_status IN ('current','carried_forward')
                          AND payment.student_id=? LIMIT 1""",
                    (int(cycle_id), int(branch_id), str(acceptance["student_id"])),
                ).fetchone()
            payment = authoritative_payment or connection.execute(
                """SELECT 1 FROM transfer_payment_aggregates
                    WHERE final_acceptance_id=? AND paid_status='paid'""",
                (int(acceptance_id),),
            ).fetchone()
            if payment is None:
                raise TransferV2AdmissionsValidationError(
                    "paid confirmation is required before waiting for COE"
                )
            receipt = self._replay_receipt(
                connection, event_uid, "start_waiting_coe", payload_json
            )
            if receipt is not None:
                connection.commit()
                return int(receipt["aggregate_id"])
            lifecycle_id = int(connection.execute(
                """INSERT INTO transfer_v2_lifecycle_events(
                     event_uid,application_cycle_id,branch_id,final_acceptance_id,event_type,note,
                     actor_user_id,occurred_at,created_at)
                   VALUES(?,?,?,?,'waiting_coe_started',NULL,?,?,?)""",
                (
                    f"{event_uid}:lifecycle",
                    int(cycle_id),
                    int(branch_id),
                    int(acceptance_id),
                    int(actor_user_id),
                    timestamp,
                    timestamp,
                ),
            ).lastrowid)
            self._record_receipt(
                connection,
                event_uid=event_uid,
                cycle_id=cycle_id,
                command_type="start_waiting_coe",
                aggregate_type="lifecycle_event",
                aggregate_id=lifecycle_id,
                payload_json=payload_json,
                actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            connection.commit()
            return lifecycle_id

    def waiting_coe_started(self, cycle_id: int, branch_id: int) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                """SELECT 1 FROM transfer_v2_lifecycle_events
                    WHERE application_cycle_id=? AND branch_id=?
                      AND event_type='waiting_coe_started'
                      AND final_acceptance_id IN (
                        SELECT id FROM cycle_final_acceptances
                         WHERE application_cycle_id=? AND acceptance_status='active'
                      ) LIMIT 1""",
                (int(cycle_id), int(branch_id), int(cycle_id)),
            ).fetchone() is not None

    def confirm_coe(
        self,
        *,
        cycle_id: int,
        branch_id: int,
        acceptance_id: int,
        coe_oshc_id: int,
        document_id: int | None,
        issued_date: str,
        actor_user_id: int,
        idempotency_key: str,
    ) -> TransferV2CoeConfirmation:
        issue_date = self._required(issued_date, "COE issued date")
        key = self._required(idempotency_key, "idempotency key")
        event_uid = self._event_uid("confirm-coe", key)
        timestamp = self.clock()
        payload_json = self._request_json({
            "cycle_id": int(cycle_id),
            "branch_id": int(branch_id),
            "acceptance_id": int(acceptance_id),
            "coe_oshc_id": int(coe_oshc_id),
            "document_id": int(document_id) if document_id is not None else None,
            "issued_date": issue_date,
        })
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._actor(connection, actor_user_id)
            acceptance = self._active_acceptance(connection, cycle_id, acceptance_id)
            if int(acceptance["transfer_branch_id"]) != int(branch_id):
                raise TransferV2AdmissionsValidationError("acceptance Branch mismatch")
            if is_new_transfer_cycle(connection, int(cycle_id)):
                confirmed_payment = connection.execute(
                    """SELECT 1
                         FROM transfer_payment_confirmations AS confirmation
                         JOIN case_record_links AS link
                           ON link.id=confirmation.payment_link_id
                         JOIN transfer_school_applications AS branch
                           ON branch.id=link.transfer_application_id
                        WHERE branch.application_cycle_id=?
                          AND link.case_id=branch.case_id
                          AND link.transfer_application_id=?
                          AND link.record_type='payment'
                          AND link.validity_status='current'
                        LIMIT 1""",
                    (int(cycle_id), int(branch_id)),
                ).fetchone()
                if confirmed_payment is None:
                    raise TransferV2AdmissionsValidationError(
                        "confirmed Payment for the accepted Branch is required before COE confirmation"
                    )
            if (
                not is_new_transfer_cycle(connection, int(cycle_id))
                and not self.waiting_coe_started_in_connection(
                    connection, cycle_id, branch_id
                )
            ):
                raise TransferV2AdmissionsValidationError(
                    "WAITING_COE_STARTED is required before COE confirmation"
                )
            receipt = self._replay_receipt(
                connection, event_uid, "confirm_coe", payload_json
            )
            if receipt is not None:
                row = connection.execute(
                    "SELECT * FROM transfer_coe_confirmations WHERE id=?",
                    (int(receipt["aggregate_id"]),),
                ).fetchone()
                connection.commit()
                return self._coe_from_row(row)
            coe = connection.execute(
                """SELECT * FROM coe_oshc
                    WHERE id=? AND student_id=? AND document_kind='COE'
                      AND application_cycle_id=?""",
                (int(coe_oshc_id), str(acceptance["student_id"]), int(cycle_id)),
            ).fetchone()
            if coe is None:
                raise TransferV2AdmissionsValidationError("COE record ownership mismatch")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_record_links'"
            ).fetchone() is not None:
                exact_link = connection.execute(
                    """SELECT 1 FROM case_record_links AS link
                         JOIN transfer_school_applications AS branch
                           ON branch.id=link.transfer_application_id
                        WHERE link.record_type='coe' AND link.record_id=?
                          AND link.transfer_application_id=?
                          AND branch.application_cycle_id=?
                          AND link.validity_status IN ('current','carried_forward')""",
                    (int(coe_oshc_id),int(branch_id),int(cycle_id)),
                ).fetchone()
                if exact_link is None:
                    raise TransferV2AdmissionsValidationError(
                        "COE does not belong to the exact Cycle and Branch"
                    )
            if document_id is not None and connection.execute(
                """SELECT 1 FROM student_documents
                    WHERE id=? AND student_id=? AND document_type='COE'""",
                (int(document_id), str(acceptance["student_id"])),
            ).fetchone() is None:
                raise TransferV2AdmissionsValidationError("COE document ownership mismatch")
            confirmation_id = int(connection.execute(
                """INSERT INTO transfer_coe_confirmations(
                     confirmation_uid,application_cycle_id,branch_id,final_acceptance_id,
                     coe_oshc_id,document_id,issued_date,status,confirmed_by,
                     confirmed_at,created_at)
                   VALUES(?,?,?,?,?,?,?,'confirmed',?,?,?)""",
                (
                    f"{event_uid}:confirmation",
                    int(cycle_id),
                    int(branch_id),
                    int(acceptance_id),
                    int(coe_oshc_id),
                    int(document_id) if document_id is not None else None,
                    issue_date,
                    int(actor_user_id),
                    timestamp,
                    timestamp,
                ),
            ).lastrowid)
            connection.execute(
                """INSERT INTO transfer_coe_events(
                     event_uid,confirmation_id,event_type,reason,actor_user_id,
                     occurred_at,created_at)
                   VALUES(?,?,'confirmed',NULL,?,?,?)""",
                (
                    f"{event_uid}:event",
                    confirmation_id,
                    int(actor_user_id),
                    timestamp,
                    timestamp,
                ),
            )
            self._append_branch_state_if_forward(
                connection,
                cycle_id=cycle_id,
                branch_id=branch_id,
                new_state="coe",
                event_type="coe_confirmed",
                actor_user_id=actor_user_id,
                timestamp=timestamp,
                event_uid=f"{event_uid}:branch",
                note=None,
            )
            self._record_receipt(
                connection,
                event_uid=event_uid,
                cycle_id=cycle_id,
                command_type="confirm_coe",
                aggregate_type="coe_confirmation",
                aggregate_id=confirmation_id,
                payload_json=payload_json,
                actor_user_id=actor_user_id,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM transfer_coe_confirmations WHERE id=?",
                (confirmation_id,),
            ).fetchone()
            connection.commit()
            return self._coe_from_row(row)

    @staticmethod
    def waiting_coe_started_in_connection(
        connection: sqlite3.Connection, cycle_id: int, branch_id: int
    ) -> bool:
        return connection.execute(
            """SELECT 1 FROM transfer_v2_lifecycle_events
                WHERE application_cycle_id=? AND branch_id=?
                  AND event_type='waiting_coe_started'
                  AND final_acceptance_id IN (
                    SELECT id FROM cycle_final_acceptances
                     WHERE application_cycle_id=? AND acceptance_status='active'
                  ) LIMIT 1""",
            (int(cycle_id), int(branch_id), int(cycle_id)),
        ).fetchone() is not None

    def current_coe_confirmation(
        self, cycle_id: int
    ) -> TransferV2CoeConfirmation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM transfer_coe_confirmations
                    WHERE application_cycle_id=? AND status='confirmed'
                    ORDER BY confirmed_at DESC,id DESC LIMIT 1""",
                (int(cycle_id),),
            ).fetchone()
            return self._coe_from_row(row) if row is not None else None
