"""Frozen, approved Workflow human decisions for controlled execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from workflow_offer_events import record_offer_received, select_final_acceptance


APPROVED_VERSION = "workflow-human-decisions-v1"
APPROVED_SHA256 = "a590d6e3b47d0a706ec667d3e9227ed4e099cc19b24300f322d23765f62ec84c"
APPLYING_DISPOSITIONS = {"APPLY_CANONICAL", "APPLY_CANONICAL_INDEPENDENT_APPLICATION"}


class FrozenDecisionInputError(ValueError):
    pass


@dataclass(frozen=True)
class FrozenDecisionSet:
    version: str
    checksum_sha256: str
    actor: str
    decisions: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class HumanDecisionPlan:
    decision_count: int
    applying_decisions: int
    projected_records: int
    decisions: FrozenDecisionSet


@dataclass(frozen=True)
class HumanDecisionApplyResult:
    decision_count: int
    applied_decisions: int
    unchanged_decisions: int
    created_records: int


def load_frozen_decisions(path: str | Path) -> FrozenDecisionSet:
    source = Path(path)
    raw = source.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if checksum != APPROVED_SHA256:
        raise FrozenDecisionInputError("human_decision_checksum_mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FrozenDecisionInputError("human_decision_input_invalid") from exc
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 25:
        raise FrozenDecisionInputError("human_decision_count_invalid")
    numbers = tuple(item.get("number") for item in decisions if isinstance(item, dict))
    if numbers != tuple(range(1, 26)):
        raise FrozenDecisionInputError("human_decision_numbers_invalid")
    if not isinstance(payload.get("actor"), str) or not payload["actor"].strip():
        raise FrozenDecisionInputError("human_decision_actor_invalid")
    return FrozenDecisionSet(
        APPROVED_VERSION,
        checksum,
        payload["actor"],
        tuple(decisions),
    )


def preview_human_decisions(
    connection: sqlite3.Connection,
    decisions: FrozenDecisionSet,
) -> HumanDecisionPlan:
    """Return the frozen plan without changing the supplied connection."""

    applying = sum(
        str(item.get("disposition")) in APPLYING_DISPOSITIONS
        for item in decisions.decisions
    )
    return HumanDecisionPlan(
        decision_count=len(decisions.decisions),
        applying_decisions=applying,
        projected_records=0,
        decisions=decisions,
    )


def _application_source(conn, decision, offer):
    cycle_id = int(decision["cycle_id"])
    student_id = str(decision["student_id"])
    school_name = str(decision["school_name"])
    school_id = decision.get("school_id")
    stamp = str(offer["uploaded_at"] or offer["accepted_date"] or offer["created_at"])
    if decision["source_kind"] == "public":
        row = conn.execute(
            """SELECT group_.id FROM public_application_groups group_
                 JOIN public_application_preferences pref
                   ON pref.public_application_group_id=group_.id
                WHERE group_.application_cycle_id=?
                  AND lower(trim(pref.school_name_snapshot))=lower(trim(?))
                ORDER BY group_.id LIMIT 1""",
            (cycle_id, school_name),
        ).fetchone()
        if row is not None:
            return "public", int(row[0]), 0
        cursor = conn.execute(
            """INSERT INTO public_application_groups(
                 application_cycle_id,student_id,city_id,status,created_at,updated_at
               ) VALUES(?,?,?,'draft',?,?)""",
            (cycle_id, student_id, offer["city_id"], stamp, stamp),
        )
        source_id = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO public_application_preferences(
                 public_application_group_id,school_id,school_name_snapshot,
                 application_status,result_at,result_note,created_at,updated_at
               ) VALUES(?,?,?,'offer_received',?,?,?,?)""",
            (
                source_id, school_id, school_name, stamp,
                "Human-confirmed final school; submission chronology remains unknown.",
                stamp, stamp,
            ),
        )
        return "public", source_id, 2
    row = conn.execute(
        """SELECT id FROM private_school_applications
            WHERE application_cycle_id=?
              AND lower(trim(school_name_snapshot))=lower(trim(?))
            ORDER BY id LIMIT 1""",
        (cycle_id, school_name),
    ).fetchone()
    if row is not None:
        return "private", int(row[0]), 0
    cursor = conn.execute(
        """INSERT INTO private_school_applications(
             application_cycle_id,student_id,school_id,school_name_snapshot,city_id,
             status,created_at,updated_at
           ) VALUES(?,?,?,?,?,'not_submitted',?,?)""",
        (cycle_id, student_id, school_id, school_name, offer["city_id"], stamp, stamp),
    )
    return "private", int(cursor.lastrowid), 1


def _apply_final_school(conn, decision, actor):
    cycle = conn.execute(
        "SELECT student_id,status FROM application_cycles WHERE id=?",
        (int(decision["cycle_id"]),),
    ).fetchone()
    if cycle is None or cycle[0] != decision["student_id"] or cycle[1] != "active":
        raise FrozenDecisionInputError(f"decision_{decision['number']}_cycle_evidence_mismatch")
    offer = conn.execute(
        """SELECT id,student_id,school_name,document_type,accepted_date,uploaded_at,
                  created_at,city_id,application_cycle_id
             FROM offers WHERE id=?""",
        (int(decision["offer_id"]),),
    ).fetchone()
    if (
        offer is None
        or offer["student_id"] != decision["student_id"]
        or offer["school_name"] != decision["school_name"]
        or offer["document_type"] != "SignedOffer"
        or int(offer["application_cycle_id"] or 0) != int(decision["cycle_id"])
    ):
        raise FrozenDecisionInputError(f"decision_{decision['number']}_offer_evidence_mismatch")
    from canonical_acceptance import active_acceptance_predicate

    active_acceptance = active_acceptance_predicate(conn, alias="acceptance")
    existing = conn.execute(
        f"""SELECT acceptance.id,acceptance.school_name_snapshot,event.legacy_offer_id
             FROM cycle_final_acceptances acceptance
             JOIN offer_received_events event ON event.id=acceptance.offer_received_event_id
            WHERE acceptance.application_cycle_id=? AND {active_acceptance}""",
        (int(decision["cycle_id"]),),
    ).fetchone()
    if existing is not None:
        if (
            existing["school_name_snapshot"] != decision["school_name"]
            or int(existing["legacy_offer_id"] or 0) != int(decision["offer_id"])
        ):
            raise FrozenDecisionInputError(f"decision_{decision['number']}_final_school_conflict")
        return 0, False
    source_kind, source_id, created = _application_source(conn, decision, offer)
    event = record_offer_received(
        conn,
        int(decision["cycle_id"]),
        external_offer_key=f"human-final-acceptance-{int(decision['offer_id'])}",
        school_name=str(decision["school_name"]),
        received_at=str(offer["uploaded_at"] or offer["accepted_date"] or offer["created_at"]),
        actor_user_id=None,
        public_group_id=source_id if source_kind == "public" else None,
        private_application_id=source_id if source_kind == "private" else None,
        actor_name=actor,
        legacy_offer_id=int(decision["offer_id"]),
        school_id=decision.get("school_id"),
    )
    select_final_acceptance(
        conn,
        int(decision["cycle_id"]),
        event.event.id,
        selected_at=str(offer["accepted_date"] or offer["uploaded_at"] or offer["created_at"]),
        actor_user_id=None,
        signed_offer_completed_at=str(offer["uploaded_at"] or offer["created_at"]),
        payment_completed_at=None,
        actor_name=actor,
    )
    return created + int(event.created) + 2, True


def _apply_contract(conn, decision, actor):
    contract_id = int(decision["contract_id"])
    student_id = str(decision["student_id"])
    contract = conn.execute(
        """SELECT student_id,contract_status,application_cycle_id,generated_at
             FROM contracts WHERE id=?""",
        (contract_id,),
    ).fetchone()
    if contract is None or contract[0] != student_id or contract[1] != "FullyExecuted":
        raise FrozenDecisionInputError("decision_25_contract_evidence_mismatch")
    rows = conn.execute(
        "SELECT id,cycle_type FROM application_cycles WHERE student_id=? ORDER BY sequence_no,id",
        (student_id,),
    ).fetchall()
    created = 0
    if rows:
        initial = [row for row in rows if row[1] == "initial"]
        if len(initial) != 1:
            raise FrozenDecisionInputError("decision_25_initial_cycle_conflict")
        cycle_id = int(initial[0][0])
    else:
        token = conn.execute(
            "SELECT token,created_at FROM parent_form_tokens WHERE student_id=? ORDER BY created_at LIMIT 1",
            (student_id,),
        ).fetchone()
        if token is None:
            raise FrozenDecisionInputError("decision_25_parent_form_evidence_missing")
        cursor = conn.execute(
            """INSERT INTO application_cycles(
                 cycle_uid,student_id,parent_form_token,cycle_type,sequence_no,status,
                 opened_at,created_by_name,created_at,updated_by_name,updated_at
               ) VALUES(?,?,?,'initial',1,'active',?,?,?,?,?)""",
            (
                "CYCLE-HUMAN-20260091-INITIAL-01", student_id, token["token"],
                token["created_at"], actor, token["created_at"], actor, token["created_at"],
            ),
        )
        cycle_id = int(cursor.lastrowid)
        created += 1
    if contract["application_cycle_id"] is None:
        conn.execute(
            "UPDATE contracts SET application_cycle_id=? WHERE id=? AND application_cycle_id IS NULL",
            (cycle_id, contract_id),
        )
        created += 1
    elif int(contract["application_cycle_id"]) != cycle_id:
        raise FrozenDecisionInputError("decision_25_contract_cycle_conflict")
    return created, bool(created)


def apply_human_decisions(
    connection: sqlite3.Connection,
    decisions: FrozenDecisionSet,
    *,
    actor: str | None = None,
) -> HumanDecisionApplyResult:
    """Apply only the frozen decisions on the caller-owned transaction."""

    applied = unchanged = created = 0
    name = actor or decisions.actor
    for decision in decisions.decisions:
        disposition = str(decision["disposition"])
        if disposition not in APPLYING_DISPOSITIONS:
            continue
        if decision["type"] == "final_accepted_school":
            count, changed = _apply_final_school(connection, decision, name)
        elif decision["type"] == "contract_ownership":
            count, changed = _apply_contract(connection, decision, name)
        else:
            raise FrozenDecisionInputError(f"decision_{decision['number']}_type_invalid")
        created += count
        applied += int(changed)
        unchanged += int(not changed)
    return HumanDecisionApplyResult(
        len(decisions.decisions), applied, unchanged, created
    )
