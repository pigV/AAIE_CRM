"""School-plan lifecycle policy and transactional service boundary.

This module owns school-plan scope and lifecycle rules.  It deliberately has
no HTTP, PDF, storage, WeChat, or global-database dependency; callers supply a
SQLite connection and retain ownership of the surrounding request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Literal, Sequence

from school_scope_resolver import resolve_school_scopes


PlanSector = Literal["public", "private"]


@dataclass(frozen=True)
class PlanScope:
    city_id: int
    city_name: str
    sector: PlanSector


class PlanScopeError(ValueError):
    def __init__(self, code: str, school_names: Sequence[str], message: str):
        super().__init__(message)
        self.code = code
        self.school_names = tuple(school_names)


@dataclass(frozen=True)
class ActivationResult:
    scope: PlanScope
    current_report_id: int
    superseded_report_ids: tuple[int, ...]


@dataclass(frozen=True)
class ParentDecisionSchool:
    school_id: int
    school_name: str
    rank_order: int | None
    is_selected: bool = True


@dataclass(frozen=True)
class ParentDecision:
    mode: Literal["ranked", "selected"]
    schools: tuple[ParentDecisionSchool, ...]


class ParentDecisionError(ValueError):
    pass


@dataclass(frozen=True)
class SubmissionState:
    confirmed_school_id: int
    student_id: str
    report_id: int
    submission_status: Literal["not_submitted", "submitted"]
    submitted_at: str | None
    submitted_by_user_id: int | None
    submitted_by_name: str | None
    unchanged: bool = False


def _unique_school_ids(school_ids: Sequence[int]) -> tuple[int, ...]:
    unique: list[int] = []
    seen: set[int] = set()
    for raw_id in school_ids:
        try:
            school_id = int(raw_id)
        except (TypeError, ValueError):
            school_id = -1
        if school_id in seen:
            continue
        seen.add(school_id)
        unique.append(school_id)
    return tuple(unique)


def infer_plan_scope(connection, school_ids: Sequence[int]) -> PlanScope:
    """Return one resolved city/sector scope or reject the whole selection."""

    unique_ids = _unique_school_ids(school_ids)
    if not unique_ids:
        raise PlanScopeError("empty_selection", (), "请至少选择一所学校。")

    rows = resolve_school_scopes(connection, unique_ids)
    rows_by_id = {row.school_id: row for row in rows}

    unknown = [school_id for school_id in unique_ids if school_id not in rows_by_id]
    if unknown:
        names = tuple(f"学校记录 #{school_id}" for school_id in unknown)
        raise PlanScopeError(
            "unknown_school",
            names,
            f"以下学校记录不存在：{'、'.join(names)}。请重新选择学校。",
        )

    ordered_rows = [rows_by_id[school_id] for school_id in unique_ids]
    missing_city = [row.school_name for row in ordered_rows if row.city_id is None or not row.city_name]
    if missing_city:
        raise PlanScopeError(
            "unresolved_city",
            missing_city,
            f"以下学校缺少城市资料：{'、'.join(missing_city)}。请先完善学校库。",
        )

    invalid_sector = [row.school_name for row in ordered_rows if row.sector_key not in {"public", "private"}]
    if invalid_sector:
        raise PlanScopeError(
            "unresolved_sector",
            invalid_sector,
            f"以下学校缺少有效的公校/私校分类：{'、'.join(invalid_sector)}。请先完善学校库。",
        )

    city_ids = {int(row.city_id) for row in ordered_rows}
    if len(city_ids) != 1:
        names = tuple(row.school_name for row in ordered_rows)
        raise PlanScopeError(
            "mixed_city",
            names,
            f"一个学校方案只能包含同一城市的学校；当前选择包含：{'、'.join(names)}。",
        )

    sectors = {str(row.sector_key).strip() for row in ordered_rows}
    if len(sectors) != 1:
        names = tuple(row.school_name for row in ordered_rows)
        raise PlanScopeError(
            "mixed_sector",
            names,
            f"一个学校方案不能混合公校和私校；当前选择包含：{'、'.join(names)}。",
        )

    first = ordered_rows[0]
    return PlanScope(
        city_id=int(first.city_id),
        city_name=str(first.city_name).strip(),
        sector=str(first.sector_key).strip(),
    )


def _actor_value(actor, key: str, default=None):
    if actor is None:
        return default
    try:
        value = actor[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(actor, key, default)
    return default if value is None else value


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _mapping_value(mapping, key: str, default=None):
    try:
        value = mapping[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(mapping, key, default)
    return default if value is None else value


def _workflow_v2_runtime_available(connection) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_cycles'"
        ).fetchone()
    )


def normalize_parent_decision(report, schools, form) -> ParentDecision:
    """Validate public rankings or private selections against one report snapshot."""

    sector = str(_mapping_value(report, "plan_sector", "") or "").strip()
    normalized_schools = tuple(
        {
            "school_id": int(_mapping_value(school, "school_id")),
            "school_name": str(_mapping_value(school, "school_name", "")),
            "rank_key": str(
                _mapping_value(
                    school,
                    "rank_key",
                    f"school_{_mapping_value(school, 'school_id')}",
                )
            ),
        }
        for school in schools
    )
    if not normalized_schools:
        raise ParentDecisionError("当前方案没有可确认的学校，请联系顾问。")

    if sector == "public":
        used_ranks: set[int] = set()
        decision_rows: list[ParentDecisionSchool] = []
        total = len(normalized_schools)
        for school in normalized_schools:
            raw_rank = _mapping_value(form, f"rank_{school['rank_key']}")
            if raw_rank in (None, ""):
                raw_rank = _mapping_value(form, f"rank_{school['school_id']}")
            text = str(raw_rank or "").strip()
            if not text.isdigit():
                raise ParentDecisionError("请完整选择学校排序。")
            rank = int(text)
            if rank < 1 or rank > total or rank in used_ranks:
                raise ParentDecisionError("学校排序不能重复，也不能漏选。")
            used_ranks.add(rank)
            decision_rows.append(
                ParentDecisionSchool(
                    school_id=school["school_id"],
                    school_name=school["school_name"],
                    rank_order=rank,
                )
            )
        if len(used_ranks) != total:
            raise ParentDecisionError("请完整选择学校排序。")
        return ParentDecision(
            mode="ranked",
            schools=tuple(sorted(decision_rows, key=lambda item: item.rank_order or 0)),
        )

    if sector == "private":
        raw_selected = _mapping_value(form, "selected_school_ids", [])
        if not isinstance(raw_selected, (list, tuple, set)):
            raw_selected = [raw_selected] if raw_selected not in (None, "") else []
        try:
            selected_ids = {int(value) for value in raw_selected}
        except (TypeError, ValueError) as exc:
            raise ParentDecisionError("选择的学校记录无效，请重新选择。") from exc
        allowed_ids = {school["school_id"] for school in normalized_schools}
        if not selected_ids:
            raise ParentDecisionError("请至少选择一所最终私校。")
        if not selected_ids.issubset(allowed_ids):
            raise ParentDecisionError("选择中包含不属于本方案的学校，请重新选择。")
        return ParentDecision(
            mode="selected",
            schools=tuple(
                ParentDecisionSchool(
                    school_id=school["school_id"],
                    school_name=school["school_name"],
                    rank_order=None,
                )
                for school in normalized_schools
                if school["school_id"] in selected_ids
            ),
        )

    raise ParentDecisionError("该学校方案尚未完成公校/私校分类，请联系顾问。")


def save_parent_confirmation(connection, context, decision: ParentDecision) -> int:
    """Persist confirmation, canonical decisions and lifecycle evidence atomically."""

    report_id = int(_mapping_value(context, "report_id"))
    report = connection.execute(
        "SELECT * FROM school_plan_reports WHERE id=?", (report_id,)
    ).fetchone()
    if report is None:
        raise LookupError("学校方案不存在。")
    expected_mode = "ranked" if str(report["plan_sector"] or "") == "public" else "selected"
    if decision.mode != expected_mode:
        raise ParentDecisionError("确认方式与学校方案类型不一致。")

    stamp = str(_mapping_value(context, "confirmed_at", _now_iso()))
    connection.execute("SAVEPOINT school_plan_parent_confirmation")
    try:
        confirmation_id = connection.execute(
            """INSERT INTO school_plan_report_confirmations(
                 report_id,viewer_id,student_id,application_id,link_id,confirmed_at,
                 confirmed_ip,user_agent,wechat_openid,wechat_unionid,wechat_nickname,
                 signature_image_path,confirmation_statement,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'confirmed',?,?)""",
            (
                report_id,
                _mapping_value(context, "viewer_id"),
                str(_mapping_value(context, "student_id")),
                str(_mapping_value(context, "application_id")),
                int(_mapping_value(context, "link_id")),
                stamp,
                str(_mapping_value(context, "confirmed_ip", "")),
                str(_mapping_value(context, "user_agent", "")),
                str(_mapping_value(context, "wechat_openid", "")),
                str(_mapping_value(context, "wechat_unionid", "")),
                str(_mapping_value(context, "wechat_nickname", "")),
                str(_mapping_value(context, "signature_image_path")),
                str(_mapping_value(context, "confirmation_statement", "")),
                stamp,
                stamp,
            ),
        ).lastrowid

        for item in decision.schools:
            if decision.mode == "ranked":
                connection.execute(
                    """INSERT INTO school_plan_report_school_rankings(
                         confirmation_id,report_id,school_id,school_name,rank_order,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        confirmation_id,
                        report_id,
                        item.school_id,
                        item.school_name,
                        item.rank_order,
                        stamp,
                    ),
                )
            connection.execute(
                """INSERT INTO school_plan_confirmed_schools(
                     confirmation_id,report_id,student_id,school_id,school_name_snapshot,
                     city_id,city_name_snapshot,plan_sector,decision_mode,rank_order,
                     is_selected,submission_status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,1,'not_submitted',?,?)""",
                (
                    confirmation_id,
                    report_id,
                    str(report["student_id"]),
                    item.school_id,
                    item.school_name,
                    report["city_id"],
                    report["city_name_snapshot"],
                    report["plan_sector"],
                    decision.mode,
                    item.rank_order,
                    stamp,
                    stamp,
                ),
            )

        connection.execute(
            """UPDATE school_plan_reports
                  SET status='confirmed',confirmed_at=?,confirmed_by_name=?,
                      confirmation_ip=?,confirmation_user_agent=?,confirmation_note=?,updated_at=?
                WHERE id=?""",
            (
                stamp,
                str(_mapping_value(context, "confirmed_by_name", "家长")),
                str(_mapping_value(context, "confirmed_ip", "")),
                str(_mapping_value(context, "user_agent", "")),
                f"Parent decision: {decision.mode}",
                stamp,
                report_id,
            ),
        )
        connection.execute(
            "UPDATE school_plan_report_links SET confirmed_at=? WHERE id=? AND report_id=?",
            (stamp, int(_mapping_value(context, "link_id")), report_id),
        )
        connection.execute(
            """INSERT INTO school_plan_report_events(
                 report_id,link_id,event_type,event_by_user_id,event_by_name,event_at,
                 ip_address,user_agent,metadata_json
               ) VALUES(?,?,?,NULL,?,?,?,?,?)""",
            (
                report_id,
                int(_mapping_value(context, "link_id")),
                "parent_decision_confirmed",
                str(_mapping_value(context, "confirmed_by_name", "家长")),
                stamp,
                str(_mapping_value(context, "confirmed_ip", "")),
                str(_mapping_value(context, "user_agent", "")),
                json.dumps(
                    {
                        "confirmation_id": confirmation_id,
                        "decision_mode": decision.mode,
                        "school_count": len(decision.schools),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        if _workflow_v2_runtime_available(connection):
            from application_cycle import attach_school_plan_confirmation

            attach_school_plan_confirmation(
                connection,
                int(confirmation_id),
                occurred_at=stamp,
                actor_user_id=None,
                actor_name=str(_mapping_value(context, "confirmed_by_name", "家长")),
            )
        connection.execute("RELEASE SAVEPOINT school_plan_parent_confirmation")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT school_plan_parent_confirmation")
        connection.execute("RELEASE SAVEPOINT school_plan_parent_confirmation")
        raise
    return int(confirmation_id)


def set_school_submission(
    connection,
    confirmed_school_id: int,
    submitted: bool,
    actor,
    occurred_at: str | None = None,
) -> SubmissionState:
    """Change one confirmed school's submission state with trigger-owned history."""

    row = connection.execute(
        "SELECT * FROM school_plan_confirmed_schools WHERE id=?",
        (int(confirmed_school_id),),
    ).fetchone()
    if row is None:
        raise LookupError("家长确认的学校记录不存在。")

    target_status = "submitted" if submitted else "not_submitted"
    current_status = str(row["submission_status"] or "not_submitted")
    if current_status == target_status:
        if _workflow_v2_runtime_available(connection):
            from application_cycle import synchronize_school_application_submission

            raw_actor_id = _actor_value(actor, "id")
            try:
                actor_id = int(raw_actor_id) if raw_actor_id is not None and int(raw_actor_id) > 0 else None
            except (TypeError, ValueError):
                actor_id = None
            actor_name = str(_actor_value(actor, "display_name", "System") or "System")
            synchronize_school_application_submission(
                connection,
                int(confirmed_school_id),
                submitted=submitted,
                occurred_at=occurred_at or row["submitted_at"] or _now_iso(),
                actor_user_id=actor_id,
                actor_name=actor_name,
            )
        return SubmissionState(
            confirmed_school_id=int(row["id"]),
            student_id=str(row["student_id"]),
            report_id=int(row["report_id"]),
            submission_status=target_status,
            submitted_at=row["submitted_at"],
            submitted_by_user_id=row["submitted_by_user_id"],
            submitted_by_name=row["submitted_by_name"],
            unchanged=True,
        )

    stamp = occurred_at or _now_iso()
    raw_actor_id = _actor_value(actor, "id")
    try:
        actor_id = int(raw_actor_id) if raw_actor_id is not None and int(raw_actor_id) > 0 else None
    except (TypeError, ValueError):
        actor_id = None
    actor_name = str(_actor_value(actor, "display_name", "System") or "System")

    connection.execute("SAVEPOINT school_plan_submission")
    try:
        if submitted:
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                      SET submission_status='submitted',submitted_at=?,
                          submitted_by_user_id=?,submitted_by_name=?,updated_at=?
                    WHERE id=?""",
                (stamp, actor_id, actor_name, stamp, int(confirmed_school_id)),
            )
        else:
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                      SET submission_status='not_submitted',submitted_at=NULL,
                          submitted_by_user_id=NULL,submitted_by_name=NULL,updated_at=?
                    WHERE id=?""",
                (stamp, int(confirmed_school_id)),
            )
        updated = connection.execute(
            "SELECT * FROM school_plan_confirmed_schools WHERE id=?",
            (int(confirmed_school_id),),
        ).fetchone()
        if _workflow_v2_runtime_available(connection):
            from application_cycle import synchronize_school_application_submission

            synchronize_school_application_submission(
                connection,
                int(confirmed_school_id),
                submitted=submitted,
                occurred_at=stamp,
                actor_user_id=actor_id,
                actor_name=actor_name,
            )
        connection.execute("RELEASE SAVEPOINT school_plan_submission")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT school_plan_submission")
        connection.execute("RELEASE SAVEPOINT school_plan_submission")
        raise

    return SubmissionState(
        confirmed_school_id=int(updated["id"]),
        student_id=str(updated["student_id"]),
        report_id=int(updated["report_id"]),
        submission_status=str(updated["submission_status"]),
        submitted_at=updated["submitted_at"],
        submitted_by_user_id=updated["submitted_by_user_id"],
        submitted_by_name=updated["submitted_by_name"],
        unchanged=False,
    )


def list_student_plans(connection, student_id: str) -> list[dict]:
    """Return lifecycle-ready school-plan view models for one student."""

    report_rows = connection.execute(
        """SELECT r.*,l.share_url,l.status link_status,l.expires_at link_expires_at,
                  l.first_viewed_at,l.last_viewed_at,l.viewed_count,
                  l.confirmed_at link_confirmed_at
             FROM school_plan_reports r
             LEFT JOIN school_plan_report_links l ON l.id=r.active_share_link_id
            WHERE r.student_id=?
            ORDER BY COALESCE(r.city_name_snapshot,''),COALESCE(r.plan_sector,''),
                     r.is_current DESC,r.report_version DESC,r.id DESC""",
        (str(student_id),),
    ).fetchall()
    if not report_rows:
        return []

    report_ids = tuple(int(row["id"]) for row in report_rows)
    placeholders = ",".join("?" for _ in report_ids)
    school_rows = connection.execute(
        f"""SELECT id,report_id,school_id,school_name_snapshot,decision_mode,
                   rank_order,is_selected,submission_status,submitted_at,submitted_by_name,
                   result_status,result_at,result_by_name,result_note,result_offer_id
              FROM school_plan_confirmed_schools
             WHERE student_id=? AND report_id IN ({placeholders})
             ORDER BY report_id,
                      CASE WHEN rank_order IS NULL THEN 2147483647 ELSE rank_order END,
                      id""",
        (str(student_id), *report_ids),
    ).fetchall()
    schools_by_report: dict[int, list[dict]] = {report_id: [] for report_id in report_ids}
    cycle_by_report = {
        int(row["id"]): row["application_cycle_id"] for row in report_rows
    }
    final_school_by_cycle: dict[int, int | None] = {}
    accepted_offer_by_cycle: dict[int, int] = {}
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_final_acceptances'"
    ).fetchone():
        from canonical_acceptance import active_acceptance_predicate

        active_acceptance = active_acceptance_predicate(connection)
        final_school_by_cycle = {
            int(row[0]): row[1]
            for row in connection.execute(
                f"""SELECT application_cycle_id,school_id
                       FROM cycle_final_acceptances
                      WHERE student_id=? AND {active_acceptance}""",
                (str(student_id),),
            )
        }
        offer_event_columns = {
            str(item[1])
            for item in connection.execute(
                "PRAGMA table_info('offer_received_events')"
            )
        }
        acceptance_columns = {
            str(item[1])
            for item in connection.execute(
                "PRAGMA table_info('cycle_final_acceptances')"
            )
        }
        if (
            "legacy_offer_id" in offer_event_columns
            and "offer_received_event_id" in acceptance_columns
        ):
            active_acceptance_for_alias = active_acceptance_predicate(
                connection, alias="acceptance"
            )
            accepted_offer_by_cycle = {
                int(item[0]): int(item[1])
                for item in connection.execute(
                    f"""SELECT acceptance.application_cycle_id,event.legacy_offer_id
                           FROM cycle_final_acceptances AS acceptance
                           JOIN offer_received_events AS event
                             ON event.id=acceptance.offer_received_event_id
                          WHERE {active_acceptance_for_alias}
                            AND event.legacy_offer_id IS NOT NULL"""
                )
            }
    offer_matching_available = bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_cycles'"
        ).fetchone()
    )
    for row in school_rows:
        offer_candidates = []
        if offer_matching_available:
            from application_cycle import list_matching_offers_for_confirmed_school

            offer_candidates = [
                {
                    "id": candidate.id,
                    "school_name": candidate.school_name,
                    "offer_date": candidate.offer_date,
                    "original_filename": candidate.original_filename,
                    "offer_status": candidate.offer_status,
                    "document_type": candidate.document_type,
                }
                for candidate in list_matching_offers_for_confirmed_school(
                    connection, int(row["id"])
                )
            ]
        candidate_ids = {int(candidate["id"]) for candidate in offer_candidates}
        cycle_id = cycle_by_report.get(int(row["report_id"]))
        accepted_offer_id = (
            accepted_offer_by_cycle.get(int(cycle_id))
            if cycle_id is not None else None
        )
        accepted_here = bool(
            accepted_offer_id is not None and accepted_offer_id in candidate_ids
        )
        linked_offer_id = (
            accepted_offer_id
            if accepted_here
            else (
                int(row["result_offer_id"])
                if row["result_offer_id"] is not None
                and int(row["result_offer_id"]) in candidate_ids
                else (int(offer_candidates[0]["id"]) if len(offer_candidates) == 1 else None)
            )
        )
        canonical_offer_received = bool(offer_candidates)
        schools_by_report[int(row["report_id"])].append(
            {
                "id": int(row["id"]),
                "school_id": row["school_id"],
                "school_name": str(row["school_name_snapshot"] or ""),
                "decision_mode": str(row["decision_mode"] or ""),
                "rank_order": row["rank_order"],
                "is_selected": bool(row["is_selected"]),
                "submission_status": str(row["submission_status"] or "not_submitted"),
                "submitted_at": row["submitted_at"],
                "submitted_by_name": row["submitted_by_name"],
                "result_status": (
                    "offer_received"
                    if canonical_offer_received
                    else str(row["result_status"] or "pending")
                ),
                "result_at": row["result_at"],
                "result_by_name": row["result_by_name"],
                "result_note": row["result_note"],
                "result_offer_id": linked_offer_id,
                "is_offer_received": canonical_offer_received,
                "is_offer_accepted": accepted_here,
                "offer_projection_source": (
                    "canonical_offer" if canonical_offer_received else "school_plan"
                ),
                "is_final_school": bool(
                    cycle_by_report.get(int(row["report_id"])) is not None
                    and row["school_id"] is not None
                    and final_school_by_cycle.get(int(cycle_by_report[int(row["report_id"])])) is not None
                    and int(final_school_by_cycle[int(cycle_by_report[int(row["report_id"])])]) == int(row["school_id"])
                ),
                "offer_candidates": offer_candidates,
            }
        )

    return [
        {
            "id": int(row["id"]),
            "student_id": str(row["student_id"]),
            "application_id": str(row["application_id"]),
            "application_cycle_id": row["application_cycle_id"],
            "report_title": str(row["report_title"] or "School Plan Report"),
            "report_version": int(row["report_version"] or 0),
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "status": str(row["status"] or ""),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "city_id": row["city_id"],
            "city_name": str(row["city_name_snapshot"] or "待确认城市"),
            "plan_sector": str(row["plan_sector"] or ""),
            "is_current": bool(row["is_current"]),
            "superseded_by_report_id": row["superseded_by_report_id"],
            "confirmed_at": row["confirmed_at"],
            "confirmed_by_name": row["confirmed_by_name"],
            "share_url": str(row["share_url"] or ""),
            "link_status": str(row["link_status"] or ""),
            "link_expires_at": row["link_expires_at"],
            "first_viewed_at": row["first_viewed_at"],
            "last_viewed_at": row["last_viewed_at"],
            "viewed_count": int(row["viewed_count"] or 0),
            "link_confirmed_at": row["link_confirmed_at"],
            "confirmed_schools": schools_by_report[int(row["id"])],
        }
        for row in report_rows
    ]


def activate_created_report(
    connection,
    report_id: int,
    actor,
    occurred_at: str | None = None,
) -> ActivationResult:
    """Resolve and activate one report atomically within its city/sector scope."""

    report = connection.execute(
        "SELECT * FROM school_plan_reports WHERE id=?", (int(report_id),)
    ).fetchone()
    if report is None:
        raise LookupError("学校方案不存在。")
    try:
        selected_school_ids = json.loads(str(report["selected_school_ids"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PlanScopeError("invalid_selection", (), "学校方案中的学校记录无法读取。") from exc
    if not isinstance(selected_school_ids, list):
        raise PlanScopeError("invalid_selection", (), "学校方案中的学校记录无法读取。")
    scope = infer_plan_scope(connection, selected_school_ids)
    stamp = occurred_at or _now_iso()
    actor_id = _actor_value(actor, "id")
    actor_name = str(_actor_value(actor, "display_name", "System"))
    superseded_ids: tuple[int, ...] = ()

    application_cycle_id = _mapping_value(report, "application_cycle_id")
    if scope.sector == "public" and application_cycle_id is not None:
        from workflow_application_service import activate_public_plan

        return activate_public_plan(
            connection,
            int(application_cycle_id),
            int(report_id),
            actor_id,
            stamp,
            actor_name,
            city_id=scope.city_id,
            city_name=scope.city_name,
        )

    connection.execute("SAVEPOINT school_plan_activation")
    try:
        if scope.sector == "public":
            prior_rows = connection.execute(
                """SELECT id, status, report_version
                     FROM school_plan_reports
                    WHERE student_id=? AND city_id=? AND plan_sector='public'
                      AND is_current=1 AND id<>?
                    ORDER BY report_version, id""",
                (report["student_id"], scope.city_id, int(report_id)),
            ).fetchall()
            superseded_ids = tuple(int(row["id"]) for row in prior_rows)
            for prior in prior_rows:
                prior_id = int(prior["id"])
                connection.execute(
                    """UPDATE school_plan_reports
                          SET status='superseded', is_current=0,
                              superseded_by_report_id=?, invalidated_at=?,
                              invalidated_reason='newer_same_city_public_plan', updated_at=?
                        WHERE id=?""",
                    (int(report_id), stamp, stamp, prior_id),
                )
                connection.execute(
                    """UPDATE school_plan_report_links
                          SET status='superseded', invalidated_at=?,
                              invalidated_reason='newer_same_city_public_plan'
                        WHERE report_id=?
                          AND status NOT IN ('expired','revoked','superseded','confirmed')""",
                    (stamp, prior_id),
                )
                connection.execute(
                    """INSERT INTO school_plan_report_events(
                         report_id,event_type,event_by_user_id,event_by_name,event_at,metadata_json
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        prior_id,
                        "report_superseded_by_same_city_public_plan",
                        actor_id,
                        actor_name,
                        stamp,
                        json.dumps(
                            {
                                "new_report_id": int(report_id),
                                "previous_status": prior["status"],
                                "previous_version": prior["report_version"],
                                "city_id": scope.city_id,
                                "city_name": scope.city_name,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

        connection.execute(
            """UPDATE school_plan_reports
                  SET city_id=?, city_name_snapshot=?, plan_sector=?, is_current=1,
                      superseded_by_report_id=NULL, scope_review_status='resolved',
                      status='active', invalidated_at=NULL, invalidated_reason=NULL,
                      updated_at=?
                WHERE id=?""",
            (scope.city_id, scope.city_name, scope.sector, stamp, int(report_id)),
        )
        connection.execute(
            """INSERT INTO school_plan_report_events(
                 report_id,event_type,event_by_user_id,event_by_name,event_at,metadata_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                int(report_id),
                "report_scope_activated",
                actor_id,
                actor_name,
                stamp,
                json.dumps(
                    {
                        "city_id": scope.city_id,
                        "city_name": scope.city_name,
                        "plan_sector": scope.sector,
                        "superseded_report_ids": list(superseded_ids),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.execute("RELEASE SAVEPOINT school_plan_activation")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT school_plan_activation")
        connection.execute("RELEASE SAVEPOINT school_plan_activation")
        raise

    return ActivationResult(
        scope=scope,
        current_report_id=int(report_id),
        superseded_report_ids=superseded_ids,
    )
