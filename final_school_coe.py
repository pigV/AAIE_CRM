"""Staff confirmation of CoE evidence for an exact final school acceptance."""

from datetime import date, datetime
import json
import re
from zoneinfo import ZoneInfo


def school_coe_rows(conn, cycle_ids):
    if not cycle_ids:
        return []
    marks = ",".join("?" for _ in cycle_ids)
    cursor = conn.execute(f"""
        SELECT acceptance.id AS acceptance_id, acceptance.application_cycle_id,
               acceptance.student_id, acceptance.school_name_snapshot,
               school.sector_key, school.state,
               coe.id AS coe_id, coe.coe_provider, coe.school_name, coe.coe_course,
               coe.coe_start_date, coe.coe_end_date, coe.file_path,
               document.id AS document_id, confirmation.evidence_snapshot
          FROM cycle_final_acceptances AS acceptance
          JOIN application_cycles AS cycle ON cycle.id=acceptance.application_cycle_id
               AND cycle.student_id=acceptance.student_id
          LEFT JOIN offer_received_events AS original_offer
            ON acceptance.school_id IS NULL
               AND original_offer.id=acceptance.offer_received_event_id
               AND original_offer.application_cycle_id=acceptance.application_cycle_id
               AND original_offer.student_id=acceptance.student_id
               AND original_offer.school_name_snapshot=acceptance.school_name_snapshot
          LEFT JOIN schools AS school
            ON school.id=coalesce(acceptance.school_id,original_offer.school_id)
          JOIN coe_oshc AS coe ON coe.application_cycle_id=cycle.id
               AND coe.student_id=cycle.student_id
          LEFT JOIN student_documents AS document ON document.id=(
               SELECT max(d.id) FROM student_documents d
                WHERE d.student_id=coe.student_id AND d.file_path=coe.file_path
                  AND d.is_active=1 AND d.document_status='active' AND d.voided_at IS NULL
          )
          LEFT JOIN final_school_coe_confirmations AS confirmation
               ON confirmation.final_acceptance_id=acceptance.id AND confirmation.coe_id=coe.id
         WHERE cycle.id IN ({marks}) AND cycle.cycle_type='initial'
           AND cycle.education_stream='school_education' AND cycle.status='active'
           AND acceptance.acceptance_status='active'
           AND upper(coe.document_kind) IN ('COE','COE_CAAW')
    """, tuple(cycle_ids))
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row)) for row in cursor]


def evidence_snapshot(row):
    return json.dumps([row[key] for key in (
        "school_name_snapshot", "sector_key", "state", "coe_provider", "school_name", "coe_course",
        "coe_start_date", "coe_end_date", "file_path", "document_id",
    )], ensure_ascii=False)


def eligibility_reason(row, today=None):
    today = today or datetime.now(ZoneInfo("Australia/Melbourne")).date()
    if not row["document_id"] or not row["file_path"]:
        return "缺少有效 CoE 附件"
    try:
        start = date.fromisoformat(row["coe_start_date"] or "")
        end = date.fromisoformat(row["coe_end_date"] or "")
    except ValueError:
        return "CoE 课程日期待核对"
    if end < today or end < start:
        return "CoE 课程已结束或日期有误"
    course = row["coe_course"] or ""
    if not course.strip() or not (row["coe_provider"] or row["school_name"] or "").strip():
        return "CoE 提供方或课程待核对"
    if re.search(r"\b(elicos|english|language|foundation|pathway|preparatory|bridging)\b|语言|预科|衔接", course, re.I):
        return "这份 CoE 属于语言或衔接课程，请核对主课 CoE"
    school_name = row["school_name_snapshot"]
    provider = row["coe_provider"] or row["school_name"]
    key = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    provider_names = re.split(r"\[|\(trading as:", provider, flags=re.I)
    if key(school_name) in {key(name) for name in provider_names}:
        return ""
    government_codes = {"VIC": "00861K", "NSW": "00588M", "SA": "00018A"}
    state_code = government_codes.get(row["state"])
    if row["sector_key"] == "public" and state_code and f"[{state_code}]" in provider.upper():
        return ""
    return "CoE 提供方与当前最终学校不匹配，请先核对学校主数据"


def confirm_school_coe(conn, student_id, acceptance_id, coe_id, actor_id, stamp):
    cycle = conn.execute(
        "SELECT application_cycle_id FROM cycle_final_acceptances WHERE id=? AND student_id=?",
        (acceptance_id, student_id),
    ).fetchone()
    rows = school_coe_rows(conn, (cycle[0],)) if cycle else []
    row = next((item for item in rows if item["coe_id"] == coe_id and item["acceptance_id"] == acceptance_id), None)
    if row is None:
        raise ValueError("CoE 与当前申请周期和最终学校不匹配")
    reason = eligibility_reason(row)
    if reason:
        raise ValueError(reason)
    conn.execute("""
        INSERT INTO final_school_coe_confirmations
            (final_acceptance_id,coe_id,evidence_snapshot,confirmed_by,confirmed_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(final_acceptance_id,coe_id) DO UPDATE SET
            evidence_snapshot=excluded.evidence_snapshot,
            confirmed_by=excluded.confirmed_by, confirmed_at=excluded.confirmed_at
        WHERE evidence_snapshot<>excluded.evidence_snapshot
    """, (acceptance_id, coe_id, evidence_snapshot(row), actor_id, stamp))
