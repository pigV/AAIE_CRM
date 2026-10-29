"""Offer school classification and accepted-Offer transaction boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from typing import Literal

from school_scope_resolver import ResolvedSchoolScope, resolve_school_scopes


ClassificationStatus = Literal["resolved", "unresolved", "ambiguous"]


@dataclass(frozen=True)
class OfferCandidate:
    school_id: int
    school_name: str
    city_id: int | None
    city_name: str | None
    sector_key: str | None


@dataclass(frozen=True)
class OfferClassification:
    status: ClassificationStatus
    school_id: int | None
    school_name: str | None
    city_id: int | None
    city_name: str | None
    sector_key: str | None
    candidates: tuple[OfferCandidate, ...] = ()


class OfferClassificationError(ValueError):
    pass


class OfferAcceptanceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AcceptanceEligibility:
    offer_id: int
    student_id: str
    eligible: bool
    reason: str | None
    coe_present: bool
    requires_coe_warning: bool


@dataclass(frozen=True)
class AcceptedOffer:
    student_id: str
    offer_id: int
    school_name: str
    status_before_acceptance: str
    accepted_at: str
    accepted_by_user_id: int | None
    accepted_by_name: str | None


@dataclass(frozen=True)
class AcceptanceChange:
    student_id: str
    offer_id: int
    previous_offer_id: int | None
    unchanged: bool
    coe_present: bool
    coe_warning_acknowledged: bool
    accepted_at: str


@dataclass(frozen=True)
class AcceptanceWithdrawal:
    student_id: str
    offer_id: int
    restored_status: str
    withdrawn_at: str
    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _value(source, key: str, default=None):
    if source is None:
        return default
    try:
        value = source[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(source, key, default)
    return default if value is None else value


def _normalize_school_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _table_columns(connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _canonical_offer_schema_active(connection) -> bool:
    return "higher_education_offer_id" in _table_columns(
        connection, "offer_received_events"
    )


def ensure_canonical_offer_event(connection, offer_id: int, actor=None):
    """Synchronize a classified formal legacy Offer into Workflow V2 once."""

    if not _canonical_offer_schema_active(connection):
        return None
    offer = connection.execute(
        "SELECT * FROM offers WHERE id=?", (int(offer_id),)
    ).fetchone()
    if offer is None:
        raise OfferAcceptanceError("offer_not_found", "Offer 记录不存在。")
    if str(_value(offer, "document_type", "Offer")) not in {"Offer", "SignedOffer"}:
        return None
    if str(_value(offer, "classification_status", "")) != "resolved":
        return None
    cycle_id = _value(offer, "application_cycle_id")
    if cycle_id is None:
        raise OfferAcceptanceError(
            "application_cycle_required",
            "Formal Offer 必须关联 ACTIVE Application Cycle。",
        )
    existing = connection.execute(
        "SELECT id FROM offer_received_events WHERE legacy_offer_id=?",
        (int(offer_id),),
    ).fetchone()
    if existing is not None:
        return int(existing[0])

    student_id = str(offer["student_id"])
    school_name = str(offer["school_name"] or "").strip()
    school_id = int(offer["school_id"])
    sector = str(offer["sector_key"] or "").strip()
    received_at = str(
        _value(offer, "uploaded_at")
        or _value(offer, "created_at")
        or _value(offer, "offer_date")
        or _now_iso()
    )
    actor_id = _value(actor, "id")
    actor_name = str(
        _value(actor, "display_name") or _value(actor, "username") or "System"
    )

    public_group_id = None
    private_application_id = None
    if sector == "public":
        source = connection.execute(
            """SELECT group_row.id
                 FROM public_application_groups AS group_row
                 JOIN public_application_preferences AS preference
                   ON preference.public_application_group_id=group_row.id
                WHERE group_row.application_cycle_id=?
                  AND (preference.school_id=?
                    OR lower(trim(preference.school_name_snapshot))=lower(trim(?)))
                ORDER BY group_row.id LIMIT 1""",
            (int(cycle_id), school_id, school_name),
        ).fetchone()
        if source is None:
            cursor = connection.execute(
                """INSERT INTO public_application_groups(
                     application_cycle_id,student_id,city_id,status,submitted_at,
                     submitted_by_user_id,submitted_by_name,created_by_user_id,
                     created_at,updated_at
                   ) VALUES(?,?,?,'submitted',?,?,?,?,?,?)""",
                (
                    int(cycle_id), student_id, offer["city_id"], received_at,
                    actor_id, actor_name, actor_id, received_at, received_at,
                ),
            )
            public_group_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO public_application_preferences(
                     public_application_group_id,school_id,school_name_snapshot,
                     preference_order,application_status,result_at,result_note,
                     created_at,updated_at
                   ) VALUES(?,?,?,1,'offer_received',?,'Created from canonical Formal Offer receipt',?,?)""",
                (
                    public_group_id, school_id, school_name, received_at,
                    received_at, received_at,
                ),
            )
        else:
            public_group_id = int(source[0])
    elif sector == "private":
        source = connection.execute(
            """SELECT id FROM private_school_applications
                WHERE application_cycle_id=?
                  AND (school_id=? OR lower(trim(school_name_snapshot))=lower(trim(?)))
                ORDER BY id LIMIT 1""",
            (int(cycle_id), school_id, school_name),
        ).fetchone()
        if source is None:
            cursor = connection.execute(
                """INSERT INTO private_school_applications(
                     application_cycle_id,student_id,school_id,school_name_snapshot,
                     city_id,status,submitted_at,submitted_by_user_id,
                     submitted_by_name,result_at,result_note,created_by_user_id,
                     created_at,updated_at
                   ) VALUES(?,?,?,?,?,'offer_received',?,?,?,?,?,?,?,?)""",
                (
                    int(cycle_id), student_id, school_id, school_name,
                    offer["city_id"], received_at, actor_id, actor_name,
                    received_at, "Created from canonical Formal Offer receipt",
                    actor_id, received_at, received_at,
                ),
            )
            private_application_id = int(cursor.lastrowid)
        else:
            private_application_id = int(source[0])
    else:
        raise OfferAcceptanceError(
            "classification_required", "Formal Offer 缺少 canonical 公校/私校分类。"
        )

    from workflow_offer_events import record_offer_received

    result = record_offer_received(
        connection,
        int(cycle_id),
        external_offer_key=f"legacy-formal-offer:{int(offer_id)}",
        school_name=school_name,
        received_at=received_at,
        actor_user_id=int(actor_id) if actor_id is not None else None,
        actor_name=actor_name,
        public_group_id=public_group_id,
        private_application_id=private_application_id,
        legacy_offer_id=int(offer_id),
        school_id=school_id,
    )
    if actor_id is not None:
        from revenue.operational_integration import process_revenue_offer_received
        process_revenue_offer_received(
            connection, result.event.id, int(actor_id), received_at
        )
    return result.event.id


def _candidate(row: ResolvedSchoolScope) -> OfferCandidate:
    return OfferCandidate(
        school_id=row.school_id,
        school_name=row.school_name,
        city_id=row.city_id,
        city_name=row.city_name,
        sector_key=row.sector_key,
    )


def _classification_from_candidates(candidates: tuple[OfferCandidate, ...]) -> OfferClassification:
    if not candidates:
        return OfferClassification("unresolved", None, None, None, None, None, ())
    if len(candidates) > 1:
        return OfferClassification("ambiguous", None, None, None, None, None, candidates)
    candidate = candidates[0]
    if candidate.city_id is None or not candidate.city_name or candidate.sector_key not in {"public", "private"}:
        return OfferClassification(
            "unresolved", None, None, None, None, None, candidates
        )
    return OfferClassification(
        "resolved",
        candidate.school_id,
        candidate.school_name,
        candidate.city_id,
        candidate.city_name,
        candidate.sector_key,
        candidates,
    )


def classify_offer_school(connection, school_name: str) -> OfferClassification:
    """Resolve only an exact normalized school name; never fuzzy-guess."""

    wanted = _normalize_school_name(school_name)
    if not wanted:
        return _classification_from_candidates(())
    rows = resolve_school_scopes(connection)
    candidates = tuple(
        _candidate(row)
        for row in rows
        if _normalize_school_name(row.school_name) == wanted
    )
    return _classification_from_candidates(candidates)


def assign_offer_school(connection, offer_id: int, school_id: int, actor) -> OfferClassification:
    """Assign a staff-selected canonical school and its validated scope to an Offer."""

    offer = connection.execute("SELECT id FROM offers WHERE id=?", (int(offer_id),)).fetchone()
    if offer is None:
        raise OfferClassificationError("Offer 记录不存在。")
    rows = resolve_school_scopes(connection, (int(school_id),))
    if not rows:
        raise OfferClassificationError("选择的学校不存在。")
    result = _classification_from_candidates((_candidate(rows[0]),))
    if result.status != "resolved":
        raise OfferClassificationError("该学校缺少有效城市或公校/私校分类，请先完善学校资料。")
    connection.execute(
        """UPDATE offers
              SET school_id=?,school_name=?,city_id=?,city_name_snapshot=?,
                  sector_key=?,classification_status='resolved'
            WHERE id=?""",
        (
            result.school_id,
            result.school_name,
            result.city_id,
            result.city_name,
            result.sector_key,
            int(offer_id),
        ),
    )
    _attach_offer_to_active_cycle(connection, int(offer_id))
    ensure_canonical_offer_event(connection, int(offer_id), actor)
    return result


def apply_offer_classification(connection, offer_id: int, school_name: str) -> OfferClassification:
    """Persist an automatic classification result without blocking normal Offer saves."""

    result = classify_offer_school(connection, school_name)
    if result.status == "resolved":
        connection.execute(
            """UPDATE offers
                  SET school_id=?,city_id=?,city_name_snapshot=?,sector_key=?,classification_status='resolved'
                WHERE id=?""",
            (result.school_id, result.city_id, result.city_name, result.sector_key, int(offer_id)),
        )
        _attach_offer_to_active_cycle(connection, int(offer_id))
        ensure_canonical_offer_event(connection, int(offer_id))
    else:
        connection.execute(
            """UPDATE offers
                  SET school_id=NULL,city_id=NULL,city_name_snapshot=NULL,sector_key=NULL,
                      classification_status=?
                WHERE id=?""",
            (result.status, int(offer_id)),
        )
    return result


def _attach_offer_to_active_cycle(connection, offer_id: int) -> None:
    """Use Workflow V2 ownership when available; classification remains usable without a cycle."""

    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_cycles'"
    ).fetchone():
        return
    offer_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info('offers')").fetchall()
    }
    if "application_cycle_id" not in offer_columns:
        return
    from application_cycle import attach_offer, get_current_cycle

    student = connection.execute(
        "SELECT student_id FROM offers WHERE id=?", (int(offer_id),)
    ).fetchone()
    if student is None or get_current_cycle(connection, str(student[0])) is None:
        return
    attach_offer(connection, int(offer_id), occurred_at=_now_iso())


def get_current_accepted_offer(connection, student_id: str) -> AcceptedOffer | None:
    row = connection.execute(
        """SELECT a.*,o.school_name
             FROM student_offer_acceptances a JOIN offers o ON o.id=a.offer_id
            WHERE a.student_id=?""",
        (str(student_id),),
    ).fetchone()
    if row is None:
        return None
    return AcceptedOffer(
        student_id=str(row["student_id"]),
        offer_id=int(row["offer_id"]),
        school_name=str(row["school_name"] or ""),
        status_before_acceptance=str(row["status_before_acceptance"]),
        accepted_at=str(row["accepted_at"]),
        accepted_by_user_id=row["accepted_by_user_id"],
        accepted_by_name=row["accepted_by_name"],
    )


def acceptance_eligibility(connection, offer_id: int) -> AcceptanceEligibility:
    offer = connection.execute("SELECT * FROM offers WHERE id=?", (int(offer_id),)).fetchone()
    if offer is None:
        raise OfferAcceptanceError("offer_not_found", "Offer 记录不存在。")
    student_id = str(offer["student_id"])
    valid_scope = (
        str(offer["classification_status"] or "") == "resolved"
        and offer["school_id"] is not None
        and offer["city_id"] is not None
        and str(offer["city_name_snapshot"] or "").strip()
        and str(offer["sector_key"] or "") in {"public", "private"}
    )
    coe_present = bool(
        connection.execute(
            """SELECT 1 FROM coe_oshc
               WHERE student_id=? AND document_kind IN ('COE','COE_CAAW') LIMIT 1""",
            (student_id,),
        ).fetchone()
    )
    current = get_current_accepted_offer(connection, student_id)
    requires_warning = bool(coe_present and current and current.offer_id != int(offer_id))
    return AcceptanceEligibility(
        offer_id=int(offer_id),
        student_id=student_id,
        eligible=bool(valid_scope),
        reason=None if valid_scope else "请先完成 Offer 学校、城市和公校/私校分类。",
        coe_present=coe_present,
        requires_coe_warning=requires_warning,
    )


def _authorized_actor(actor) -> tuple[int | None, str]:
    if not bool(_value(actor, "can_manage_offer", False)):
        raise OfferAcceptanceError("access_denied", "你没有设置已接受 Offer 的权限。")
    raw_id = _value(actor, "id")
    try:
        actor_id = int(raw_id) if raw_id is not None and int(raw_id) > 0 else None
    except (TypeError, ValueError):
        actor_id = None
    return actor_id, str(_value(actor, "display_name", "System") or "System")


def set_accepted_offer(
    connection,
    student_id: str,
    offer_id: int,
    actor,
    warning_acknowledged: bool = False,
    occurred_at: str | None = None,
) -> AcceptanceChange:
    """Set exactly one accepted Offer, preserving prior status and permanent history."""

    actor_id, actor_name = _authorized_actor(actor)
    offer = connection.execute("SELECT * FROM offers WHERE id=?", (int(offer_id),)).fetchone()
    if offer is None:
        raise OfferAcceptanceError("offer_not_found", "Offer 记录不存在。")
    if str(offer["student_id"]) != str(student_id):
        raise OfferAcceptanceError("student_mismatch", "该 Offer 不属于当前学生。")
    eligibility = acceptance_eligibility(connection, int(offer_id))
    if not eligibility.eligible:
        raise OfferAcceptanceError("classification_required", eligibility.reason or "Offer 学校资料不完整。")
    current = get_current_accepted_offer(connection, str(student_id))
    if current and current.offer_id == int(offer_id):
        return AcceptanceChange(
            student_id=str(student_id),
            offer_id=int(offer_id),
            previous_offer_id=int(offer_id),
            unchanged=True,
            coe_present=eligibility.coe_present,
            coe_warning_acknowledged=bool(warning_acknowledged),
            accepted_at=current.accepted_at,
        )
    if current and current.status_before_acceptance == "__legacy_status_unknown__":
        raise OfferAcceptanceError(
            "legacy_restore_status_required",
            "当前已接受 Offer 的历史状态待检查；请先完成历史状态复核，再更换 Offer。",
        )
    if eligibility.requires_coe_warning and not warning_acknowledged:
        raise OfferAcceptanceError(
            "coe_warning_required",
            "该学生已经有 COE。更换已接受 Offer 可能影响 COE，请确认风险后再次提交。",
        )

    stamp = occurred_at or _now_iso()
    previous_offer_id = current.offer_id if current else None
    new_previous_status = str(offer["offer_status"] or "received")
    if new_previous_status == "accepted":
        raise OfferAcceptanceError(
            "inconsistent_accepted_status",
            "该 Offer 状态异常，请先联系管理员检查历史接受记录。",
        )

    connection.execute("SAVEPOINT offer_acceptance_change")
    try:
        restored_status = None
        if current:
            restored_status = current.status_before_acceptance
            connection.execute(
                """UPDATE offers
                      SET offer_status=?,accepted_date=NULL,accepted_by=NULL
                    WHERE id=? AND student_id=?""",
                (restored_status, current.offer_id, str(student_id)),
            )
        connection.execute(
            """UPDATE offers SET offer_status='accepted',accepted_date=?,accepted_by=?
                WHERE id=? AND student_id=?""",
            (stamp[:10], actor_id, int(offer_id), str(student_id)),
        )
        connection.execute(
            """INSERT INTO student_offer_acceptances(
                 student_id,offer_id,status_before_acceptance,accepted_at,
                 accepted_by_user_id,accepted_by_name,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(student_id) DO UPDATE SET
                 offer_id=excluded.offer_id,
                 status_before_acceptance=excluded.status_before_acceptance,
                 accepted_at=excluded.accepted_at,
                 accepted_by_user_id=excluded.accepted_by_user_id,
                 accepted_by_name=excluded.accepted_by_name,
                 updated_at=excluded.updated_at""",
            (str(student_id), int(offer_id), new_previous_status, stamp, actor_id, actor_name, stamp),
        )
        connection.execute(
            """INSERT INTO offer_acceptance_history(
                 student_id,previous_offer_id,new_offer_id,previous_offer_restored_status,
                 new_offer_previous_status,accepted_by_user_id,accepted_by_name,accepted_at,
                 coe_present,coe_warning_acknowledged,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(student_id), previous_offer_id, int(offer_id), restored_status,
                new_previous_status, actor_id, actor_name, stamp,
                1 if eligibility.coe_present else 0,
                1 if warning_acknowledged else 0,
                stamp,
            ),
        )
        if _canonical_offer_schema_active(connection):
            from workflow_offer_events import (
                change_final_acceptance,
                select_final_acceptance,
            )

            canonical_offer_id = ensure_canonical_offer_event(
                connection, int(offer_id), actor
            )
            if canonical_offer_id is None:
                raise OfferAcceptanceError(
                    "formal_offer_required",
                    "只有 canonical Formal Offer 可以设为最终接受。",
                )
            cycle_id = int(offer["application_cycle_id"])
            canonical_current = connection.execute(
                """SELECT offer_received_event_id
                     FROM cycle_final_acceptances
                    WHERE application_cycle_id=? AND acceptance_status='active'""",
                (cycle_id,),
            ).fetchone()
            if canonical_current is None:
                select_final_acceptance(
                    connection,
                    cycle_id,
                    canonical_offer_id,
                    selected_at=stamp,
                    actor_user_id=actor_id,
                    actor_name=actor_name,
                )
            elif int(canonical_current[0]) != int(canonical_offer_id):
                change_final_acceptance(
                    connection,
                    cycle_id,
                    canonical_offer_id,
                    occurred_at=stamp,
                    actor_user_id=actor_id,
                    actor_name=actor_name,
                    reason="Employee changed accepted Offer",
                )
            active_acceptance = connection.execute(
                """SELECT id FROM cycle_final_acceptances
                    WHERE application_cycle_id=? AND acceptance_status='active'""",
                (cycle_id,),
            ).fetchone()
            if active_acceptance is not None and actor_id is not None:
                from revenue.operational_integration import process_revenue_final_acceptance
                process_revenue_final_acceptance(
                    connection, int(active_acceptance[0]), int(actor_id), stamp
                )
        connection.execute("RELEASE SAVEPOINT offer_acceptance_change")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT offer_acceptance_change")
        connection.execute("RELEASE SAVEPOINT offer_acceptance_change")
        raise

    return AcceptanceChange(
        student_id=str(student_id),
        offer_id=int(offer_id),
        previous_offer_id=previous_offer_id,
        unchanged=False,
        coe_present=eligibility.coe_present,
        coe_warning_acknowledged=bool(warning_acknowledged),
        accepted_at=stamp,
    )


def withdraw_accepted_offer(
    connection,
    student_id: str,
    actor,
    *,
    reason: str,
    occurred_at: str | None = None,
) -> AcceptanceWithdrawal:
    """Explicitly withdraw the current acceptance while retaining canonical history."""

    actor_id, actor_name = _authorized_actor(actor)
    current = get_current_accepted_offer(connection, str(student_id))
    if current is None:
        raise OfferAcceptanceError(
            "active_acceptance_not_found", "当前没有可撤回的已接受 Offer。"
        )
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise OfferAcceptanceError("reason_required", "撤回 Acceptance 必须填写原因。")
    if current.status_before_acceptance == "__legacy_status_unknown__":
        raise OfferAcceptanceError(
            "legacy_restore_status_required",
            "当前已接受 Offer 的历史状态待检查，不能自动撤回。",
        )
    offer = connection.execute(
        "SELECT * FROM offers WHERE id=? AND student_id=?",
        (current.offer_id, str(student_id)),
    ).fetchone()
    if offer is None:
        raise OfferAcceptanceError("offer_not_found", "Offer 记录不存在。")
    stamp = occurred_at or _now_iso()
    connection.execute("SAVEPOINT withdraw_offer_acceptance")
    try:
        connection.execute(
            """UPDATE offers
                  SET offer_status=?,accepted_date=NULL,accepted_by=NULL
                WHERE id=? AND student_id=?""",
            (current.status_before_acceptance, current.offer_id, str(student_id)),
        )
        connection.execute(
            "DELETE FROM student_offer_acceptances WHERE student_id=? AND offer_id=?",
            (str(student_id), current.offer_id),
        )
        if _canonical_offer_schema_active(connection):
            from workflow_offer_events import withdraw_final_acceptance

            cycle_id = _value(offer, "application_cycle_id")
            if cycle_id is None:
                raise OfferAcceptanceError(
                    "application_cycle_required",
                    "Acceptance 缺少 canonical Application Cycle。",
                )
            withdraw_final_acceptance(
                connection,
                int(cycle_id),
                occurred_at=stamp,
                actor_user_id=actor_id,
                actor_name=actor_name,
                reason=cleaned_reason,
            )
        connection.execute("RELEASE SAVEPOINT withdraw_offer_acceptance")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT withdraw_offer_acceptance")
        connection.execute("RELEASE SAVEPOINT withdraw_offer_acceptance")
        raise
    return AcceptanceWithdrawal(
        student_id=str(student_id),
        offer_id=current.offer_id,
        restored_status=current.status_before_acceptance,
        withdrawn_at=stamp,
        reason=cleaned_reason,
    )
