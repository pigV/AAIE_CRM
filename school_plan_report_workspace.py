from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any


VALID_FILTERS = ("needs_followup", "opened", "unopened", "confirmed", "all")
FILTER_LABELS = {
    "needs_followup": "需要跟进",
    "opened": "已打开",
    "unopened": "未打开",
    "confirmed": "已确认",
    "all": "全部",
}


def _row_dict(row) -> dict[str, Any]:
    return dict(row) if not isinstance(row, dict) else dict(row)


def _school_count(value) -> int:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    return len(parsed) if isinstance(parsed, list) else 0


def _report_activity(row: Mapping[str, Any]) -> str:
    return str(
        row.get("last_viewed_at")
        or row.get("confirmed_at")
        or row.get("updated_at")
        or row.get("created_at")
        or ""
    )


def _is_confirmed(row: Mapping[str, Any], parent_status: str) -> bool:
    return bool(
        row.get("confirmed_at")
        or row.get("link_confirmed_at")
        or parent_status == "confirmed"
        or row.get("status") == "confirmed"
    )


def _operational_state(row: Mapping[str, Any], parent_status: str) -> str:
    if _is_confirmed(row, parent_status):
        return "confirmed"
    if parent_status in {"opened", "logged_in"}:
        return "needs_followup"
    if parent_status == "not_opened":
        return "unopened"
    return "no_link"


def _state_priority(state: str) -> int:
    return {
        "needs_followup": 0,
        "no_link": 1,
        "unopened": 2,
        "confirmed": 3,
        "no_current": 4,
    }.get(state, 5)


def _matches_filter(current_plans: list[dict[str, Any]], status_filter: str) -> bool:
    return status_filter == "all" or any(
        _plan_matches_filter(plan, status_filter) for plan in current_plans
    )


def _plan_matches_filter(plan: Mapping[str, Any], status_filter: str) -> bool:
    if status_filter == "all":
        return True
    if status_filter == "opened":
        return plan["parent_status"] in {"opened", "logged_in", "confirmed"}
    return plan["operational_state"] == status_filter


def _project_report(row, link_status, display_datetime) -> dict[str, Any]:
    row = _row_dict(row)
    parent_status = str(link_status(row) or "missing")
    operational_state = _operational_state(row, parent_status)
    return {
        "report_id": int(row["id"]),
        "student_id": str(row.get("student_id") or ""),
        "application_id": str(row.get("application_id") or ""),
        "title": str(row.get("report_title") or "School Plan Report"),
        "version": int(row.get("report_version") or 0),
        "sector": str(row.get("plan_sector") or ""),
        "city_id": row.get("city_id"),
        "city_name": str(row.get("city_name_snapshot") or "待确认城市"),
        "is_current": bool(int(row.get("is_current") or 0)),
        "scope_review_status": str(row.get("scope_review_status") or "pending"),
        "superseded_by_report_id": row.get("superseded_by_report_id"),
        "status": str(row.get("status") or ""),
        "parent_status": parent_status,
        "operational_state": operational_state,
        "share_url": str(row.get("share_url") or ""),
        "can_copy_link": bool(row.get("share_url"))
        and parent_status in {"not_opened", "opened", "logged_in", "confirmed"},
        "viewed_count": int(row.get("viewed_count") or 0),
        "last_viewed_at": str(row.get("last_viewed_at") or ""),
        "last_viewed_display": display_datetime(row.get("last_viewed_at"))
        if row.get("last_viewed_at")
        else "",
        "confirmed_at": str(row.get("confirmed_at") or row.get("link_confirmed_at") or ""),
        "confirmed_display": display_datetime(
            row.get("confirmed_at") or row.get("link_confirmed_at")
        )
        if (row.get("confirmed_at") or row.get("link_confirmed_at"))
        else "",
        "confirmed_by_name": str(row.get("confirmed_by_name") or ""),
        "school_count": _school_count(row.get("selected_school_ids")),
        "file_name": str(row.get("file_name") or ""),
        "has_pdf": bool(row.get("file_path")),
        "detail_url": f"/school-plan-reports/{int(row['id'])}",
        "preview_url": f"/school-plan-reports/{int(row['id'])}/preview",
        "download_url": f"/school-plan-reports/{int(row['id'])}/download"
        if row.get("file_path")
        else "",
        "created_by_name": str(row.get("created_by_name") or ""),
        "created_display": display_datetime(row.get("created_at"))
        if row.get("created_at")
        else "",
        "updated_at": str(row.get("updated_at") or ""),
        "activity_at": _report_activity(row),
    }


def load_school_plan_report_workspace(
    database,
    *,
    link_status: Callable[[Mapping[str, Any]], str],
    display_datetime: Callable[[object], str],
    search: str = "",
    status_filter: str = "needs_followup",
    page: int = 1,
    page_size: int = 20,
    selected_report_id: int | None = None,
) -> dict[str, object]:
    status_filter = status_filter if status_filter in VALID_FILTERS else "needs_followup"
    search_text = str(search or "").strip()
    page_size = max(1, min(int(page_size or 20), 50))
    selected_report_id = (
        int(selected_report_id) if str(selected_report_id or "").isdigit() else None
    )

    state_ctes = """
        WITH raw_states AS (
          SELECT r.id,r.student_id,r.application_id,r.is_current,r.status,
                 COALESCE(
                   NULLIF(TRIM(COALESCE(s.passport_first_name,'') || ' ' ||
                               COALESCE(s.passport_last_name,'')),''),
                   r.student_id,'') student_name,
                 COALESCE(NULLIF(l.last_viewed_at,''),NULLIF(r.confirmed_at,''),
                          NULLIF(r.updated_at,''),NULLIF(r.created_at,''),'') activity_at,
                 CASE
                   WHEN COALESCE(l.share_url,'')='' THEN 'missing'
                   WHEN l.status='revoked' THEN 'revoked'
                   WHEN l.status='confirmed' OR COALESCE(l.confirmed_at,'')<>''
                     THEN 'confirmed'
                   WHEN COALESCE(l.expires_at,'')='' OR
                        julianday(l.expires_at)<julianday('now') THEN 'expired'
                   WHEN COALESCE(l.bound_wechat_openid,'')<>'' THEN 'logged_in'
                   WHEN COALESCE(l.first_viewed_at,'')<>'' THEN 'opened'
                   ELSE 'not_opened'
                 END parent_status
            FROM school_plan_reports r
            LEFT JOIN students s ON s.student_id=r.student_id
            LEFT JOIN school_plan_report_links l ON l.id=r.active_share_link_id
           WHERE (?='' OR instr(
                    lower(COALESCE(
                      NULLIF(TRIM(COALESCE(s.passport_first_name,'') || ' ' ||
                                  COALESCE(s.passport_last_name,'')),''),
                      r.student_id,'') || ' ' || r.student_id),
                    lower(?)
                 )>0)
        ),
        report_states AS (
          SELECT *,
                 CASE
                   WHEN COALESCE(status,'')='confirmed' OR parent_status='confirmed'
                     THEN 'confirmed'
                   WHEN parent_status IN ('opened','logged_in') THEN 'needs_followup'
                   WHEN parent_status='not_opened' THEN 'unopened'
                   ELSE 'no_link'
                 END operational_state
            FROM raw_states
        )
    """
    count_rows = database.all(
        state_ctes
        + """,
        grouped AS (
          SELECT student_id,application_id,
                 MAX(CASE WHEN is_current=1 AND operational_state='needs_followup'
                          THEN 1 ELSE 0 END) needs_followup_count,
                 MAX(CASE WHEN is_current=1 AND
                                   parent_status IN ('opened','logged_in','confirmed')
                          THEN 1 ELSE 0 END) opened_count,
                 MAX(CASE WHEN is_current=1 AND operational_state='unopened'
                          THEN 1 ELSE 0 END) unopened_count,
                 MAX(CASE WHEN is_current=1 AND operational_state='confirmed'
                          THEN 1 ELSE 0 END) confirmed_count
            FROM report_states
           GROUP BY student_id,application_id
        )
        SELECT COALESCE(SUM(needs_followup_count),0) needs_followup,
               COALESCE(SUM(opened_count),0) opened,
               COALESCE(SUM(unopened_count),0) unopened,
               COALESCE(SUM(confirmed_count),0) confirmed,
               COUNT(*) all_count
          FROM grouped""",
        (search_text, search_text),
    )
    count_row = _row_dict(count_rows[0]) if count_rows else {}
    filter_counts = {
        "needs_followup": int(count_row.get("needs_followup") or 0),
        "opened": int(count_row.get("opened") or 0),
        "unopened": int(count_row.get("unopened") or 0),
        "confirmed": int(count_row.get("confirmed") or 0),
        "all": int(count_row.get("all_count") or 0),
    }
    total_groups = filter_counts[status_filter]
    page_count = max(1, math.ceil(total_groups / page_size))
    page = max(1, min(int(page or 1), page_count))

    eligibility = {
        "needs_followup": "is_current=1 AND operational_state='needs_followup'",
        "opened": "is_current=1 AND parent_status IN ('opened','logged_in','confirmed')",
        "unopened": "is_current=1 AND operational_state='unopened'",
        "confirmed": "is_current=1 AND operational_state='confirmed'",
        "all": "(current_plan_count=0 OR is_current=1)",
    }[status_filter]
    page_rows = database.all(
        state_ctes
        + f""",
        enriched AS (
          SELECT *,SUM(CASE WHEN is_current=1 THEN 1 ELSE 0 END)
                       OVER (PARTITION BY student_id,application_id) current_plan_count
            FROM report_states
        ),
        eligible AS (
          SELECT *,CASE WHEN current_plan_count=0 THEN 'no_current'
                        ELSE operational_state END summary_status
            FROM enriched
           WHERE {eligibility}
        ),
        ranked AS (
          SELECT *,ROW_NUMBER() OVER (
                   PARTITION BY student_id,application_id
                   ORDER BY CASE summary_status
                              WHEN 'needs_followup' THEN 0
                              WHEN 'no_link' THEN 1
                              WHEN 'unopened' THEN 2
                              WHEN 'confirmed' THEN 3
                              WHEN 'no_current' THEN 4 ELSE 5 END,
                            activity_at DESC,id DESC
                 ) group_rank
            FROM eligible
        )
        SELECT student_id,application_id,student_name,
               id summary_report_id,summary_status, parent_status summary_parent_status,
               activity_at summary_activity_at,current_plan_count
          FROM ranked
         WHERE group_rank=1
         ORDER BY CASE summary_status
                    WHEN 'needs_followup' THEN 0
                    WHEN 'no_link' THEN 1
                    WHEN 'unopened' THEN 2
                    WHEN 'confirmed' THEN 3
                    WHEN 'no_current' THEN 4 ELSE 5 END,
                  summary_activity_at DESC,summary_report_id DESC
         LIMIT ? OFFSET ?""",
        (search_text, search_text, page_size, (page - 1) * page_size),
    )
    page_metadata = [_row_dict(row) for row in page_rows]
    page_group_keys = [
        (str(row["student_id"]), str(row["application_id"]))
        for row in page_metadata
    ]
    hydrated_by_key: dict[str, dict[str, Any]] = {}
    if page_group_keys:
        predicates = " OR ".join(
            "(r.student_id=? AND r.application_id=?)" for _ in page_group_keys
        )
        params = tuple(value for key in page_group_keys for value in key) + (-1,)
        page_rows = database.all(
            f"""SELECT r.id,r.student_id,r.application_id,r.report_title,
                       r.report_version,r.selected_school_ids,r.file_name,r.file_path,
                       r.status,r.city_id,r.city_name_snapshot,r.plan_sector,
                       r.is_current,r.superseded_by_report_id,r.scope_review_status,
                       r.confirmed_at,r.confirmed_by_name,r.created_by_name,
                       r.created_at,r.updated_at,
                       s.passport_first_name,s.passport_last_name,
                       l.status link_status,l.share_url,l.expires_at,
                       l.viewed_count,l.last_viewed_at,
                       l.confirmed_at link_confirmed_at,
                       l.bound_wechat_openid link_bound_wechat_openid,
                       l.first_viewed_at link_first_viewed_at
                  FROM school_plan_reports r
                  LEFT JOIN students s ON s.student_id=r.student_id
                  LEFT JOIN school_plan_report_links l ON l.id=r.active_share_link_id
                 WHERE {predicates}
                 ORDER BY r.updated_at DESC,r.id DESC LIMIT ?""",
            params,
        )
        hydrated = build_school_plan_report_workspace(
            page_rows,
            link_status=link_status,
            display_datetime=display_datetime,
            search="",
            status_filter="all",
            page=1,
            page_size=50,
            selected_report_id=selected_report_id,
        )
        hydrated_by_key = {
            str(group["group_key"]): group for group in hydrated["groups"]
        }

    groups = []
    for metadata in page_metadata:
        group_key = f"{metadata['student_id']}::{metadata['application_id']}"
        group = hydrated_by_key.get(group_key)
        if not group:
            continue
        group.update(
            {
                "summary_report_id": int(metadata["summary_report_id"]),
                "summary_status": str(metadata["summary_status"]),
                "summary_parent_status": str(metadata["summary_parent_status"]),
                "summary_activity_at": str(metadata["summary_activity_at"] or ""),
                "summary_activity_display": next(
                    (
                        item["last_viewed_display"]
                        or item["confirmed_display"]
                        or item["created_display"]
                        for item in group["records"]
                        if item["report_id"] == int(metadata["summary_report_id"])
                    ),
                    "",
                ),
                "current_plan_count": int(metadata["current_plan_count"] or 0),
            }
        )
        groups.append(group)

    selected_group = next(
        (
            group
            for group in groups
            if any(
                item["report_id"] == selected_report_id for item in group["records"]
            )
        ),
        groups[0] if groups else None,
    )
    selected_report = None
    if selected_group:
        selected_report = next(
            (
                item
                for item in selected_group["current_plans"]
                if item["report_id"] == selected_report_id
            ),
            None,
        )
        if selected_report is None and selected_report_id is None:
            selected_report = next(
                (
                    item
                    for item in selected_group["current_plans"]
                    if item["report_id"] == selected_group["summary_report_id"]
                ),
                None,
            )
        elif selected_report is None and not any(
            item["report_id"] == selected_report_id for item in selected_group["records"]
        ):
            selected_report = next(
                (
                    item
                    for item in selected_group["current_plans"]
                    if item["report_id"] == selected_group["summary_report_id"]
                ),
                None,
            )

    selected = None
    if selected_group:
        selected = {
            "group_key": selected_group["group_key"],
            "student_id": selected_group["student_id"],
            "application_id": selected_group["application_id"],
            "student_name": selected_group["student_name"],
            "create_url": selected_group["create_url"],
            "current_plans": selected_group["current_plans"],
            "records": selected_group["records"],
            "selected_report": selected_report,
        }

    return {
        "search": search_text,
        "status_filter": status_filter,
        "filters": [
            {"key": key, "label": FILTER_LABELS[key], "count": filter_counts[key]}
            for key in VALID_FILTERS
        ],
        "groups": groups,
        "page": page,
        "page_count": page_count,
        "total_groups": total_groups,
        "selected": selected,
        "selected_report_id": (
            selected_report["report_id"]
            if selected_report
            else selected_group["summary_report_id"]
            if selected_group
            else None
        ),
    }


def build_school_plan_report_workspace(
    report_rows,
    *,
    link_status: Callable[[Mapping[str, Any]], str],
    display_datetime: Callable[[object], str],
    search: str = "",
    status_filter: str = "needs_followup",
    page: int = 1,
    page_size: int = 20,
    selected_report_id: int | None = None,
) -> dict[str, object]:
    status_filter = status_filter if status_filter in VALID_FILTERS else "needs_followup"
    search_text = str(search or "").strip()
    search_key = search_text.casefold()
    page_size = max(1, min(int(page_size or 20), 50))
    selected_report_id = (
        int(selected_report_id) if str(selected_report_id or "").isdigit() else None
    )

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for source in report_rows:
        row = _row_dict(source)
        student_name = " ".join(
            part
            for part in (
                str(row.get("passport_first_name") or "").strip(),
                str(row.get("passport_last_name") or "").strip(),
            )
            if part
        ) or str(row.get("student_id") or "")
        key = (str(row.get("student_id") or ""), str(row.get("application_id") or ""))
        group = grouped.setdefault(
            key,
            {
                "group_key": f"{key[0]}::{key[1]}",
                "student_id": key[0],
                "application_id": key[1],
                "student_name": student_name,
                "records": [],
            },
        )
        group["records"].append(_project_report(row, link_status, display_datetime))

    groups = []
    for group in grouped.values():
        if search_key and search_key not in f"{group['student_name']} {group['student_id']}".casefold():
            continue
        records = sorted(
            group["records"],
            key=lambda item: (
                str(item["city_name"]),
                {"public": 0, "private": 1}.get(str(item["sector"]), 2),
                -int(item["is_current"]),
                -int(item["version"]),
                -int(item["report_id"]),
            ),
        )
        current_plans = [item for item in records if item["is_current"]]
        if current_plans:
            ranked_current = sorted(
                current_plans, key=lambda item: int(item["report_id"]), reverse=True
            )
            ranked_current.sort(key=lambda item: str(item["activity_at"] or ""), reverse=True)
            ranked_current.sort(key=lambda item: _state_priority(item["operational_state"]))
            summary = ranked_current[0]
            summary_status = summary["operational_state"]
        else:
            summary = max(records, key=lambda item: (item["activity_at"], item["report_id"]))
            summary_status = "no_current"
        group.update(
            {
                "records": records,
                "current_plans": current_plans,
                "summary_report_id": summary["report_id"],
                "summary_status": summary_status,
                "summary_parent_status": summary["parent_status"],
                "summary_activity_at": summary["activity_at"],
                "summary_activity_display": summary["last_viewed_display"]
                or summary["confirmed_display"]
                or summary["created_display"],
                "current_plan_count": len(current_plans),
                "create_url": f"/school-plan-reports/create?student_id={group['student_id']}",
            }
        )
        groups.append(group)

    filter_counts = {
        key: sum(1 for group in groups if _matches_filter(group["current_plans"], key))
        for key in VALID_FILTERS
    }
    filtered = [
        group
        for group in groups
        if _matches_filter(group["current_plans"], status_filter)
    ]
    for group in filtered:
        matching_current = [
            plan
            for plan in group["current_plans"]
            if _plan_matches_filter(plan, status_filter)
        ]
        matching_current.sort(key=lambda item: int(item["report_id"]), reverse=True)
        matching_current.sort(key=lambda item: str(item["activity_at"] or ""), reverse=True)
        matching_current.sort(key=lambda item: _state_priority(item["operational_state"]))
        if matching_current:
            summary = matching_current[0]
            group["summary_report_id"] = summary["report_id"]
            group["summary_status"] = summary["operational_state"]
            group["summary_parent_status"] = summary["parent_status"]
            group["summary_activity_at"] = summary["activity_at"]
            group["summary_activity_display"] = (
                summary["last_viewed_display"]
                or summary["confirmed_display"]
                or summary["created_display"]
            )
    filtered.sort(key=lambda group: int(group["summary_report_id"]), reverse=True)
    filtered.sort(key=lambda group: str(group["summary_activity_at"] or ""), reverse=True)
    filtered.sort(key=lambda group: _state_priority(group["summary_status"]))
    page_count = max(1, math.ceil(len(filtered) / page_size))
    page = max(1, min(int(page or 1), page_count))
    page_groups = filtered[(page - 1) * page_size : page * page_size]

    selected_group = None
    selected_report = None
    for group in page_groups:
        requested_group_record = next(
            (
                item
                for item in group["records"]
                if item["report_id"] == selected_report_id
            ),
            None,
        )
        if requested_group_record:
            selected_group = group
            selected_report = next(
                (
                    item
                    for item in group["current_plans"]
                    if item["report_id"] == selected_report_id
                ),
                None,
            )
            break
    if selected_group is None and page_groups:
        selected_group = page_groups[0]
        selected_report = next(
            item
            for item in selected_group["current_plans"]
            if item["report_id"] == selected_group["summary_report_id"]
        ) if selected_group["current_plans"] else None

    selected = None
    if selected_group:
        selected = {
            "group_key": selected_group["group_key"],
            "student_id": selected_group["student_id"],
            "application_id": selected_group["application_id"],
            "student_name": selected_group["student_name"],
            "create_url": selected_group["create_url"],
            "current_plans": selected_group["current_plans"],
            "records": selected_group["records"],
            "selected_report": selected_report,
        }

    return {
        "search": search_text,
        "status_filter": status_filter,
        "filters": [
            {"key": key, "label": FILTER_LABELS[key], "count": filter_counts[key]}
            for key in VALID_FILTERS
        ],
        "groups": page_groups,
        "page": page,
        "page_count": page_count,
        "total_groups": len(filtered),
        "selected": selected,
        "selected_report_id": (
            selected_report["report_id"]
            if selected_report
            else selected_group["summary_report_id"]
            if selected_group
            else None
        ),
    }
