"""Staff confirmation of CoE evidence for an exact final school acceptance."""

from datetime import date, datetime
import json
import re
from zoneinfo import ZoneInfo
from business_changes.coe import synchronized_coe_confirmation


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
    if re.search(r"\b(elicos|english|language|foundation|pathway|preparatory|preparation|bridging)\b|语言|预科|衔接", course, re.I):
        return "这份 CoE 属于语言或衔接课程，请核对主课 CoE"
    school_name = row["school_name_snapshot"]
    provider = row["coe_provider"] or row["school_name"]
    key = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    provider_names = re.split(r"\[|\(trading as:", provider, flags=re.I)
    # A CRICOS provider may explicitly list several trading names. Compare
    # whole names in that clause only; a shared city or a substring is not an
    # identity, and an unlabelled 'and' is not evidence of an alias list.
    trading = re.search(r"\(trading\s+as\s*:\s*([^)]*)", provider, re.I)
    if trading:
        provider_names.extend(re.split(r"\s+and\s+", trading.group(1), flags=re.I))
    if key(school_name) in {key(name) for name in provider_names}:
        return ""
    government_codes = {"VIC": "00861K", "NSW": "00588M", "SA": "00018A"}
    state_code = government_codes.get(row["state"])
    if row["sector_key"] == "public" and state_code and f"[{state_code}]" in provider.upper():
        return ""
    return "CoE 提供方与当前最终学校不匹配，请先核对学校主数据"


@synchronized_coe_confirmation
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


def historical_school_coe_rows(conn, student_id, *, today=None):
    """Find expired offers with signed evidence and a matching CoE uploaded in time."""
    from dashboard_business_time import business_date_iso

    today = today or datetime.now(ZoneInfo("Australia/Melbourne")).date()
    cursor = conn.execute("""
        SELECT cycle.id AS application_cycle_id, receipt.id AS offer_event_id,
               receipt.school_name_snapshot, school.state, school.sector_key,
               offer.offer_date, offer.offer_expiry_date,
               signed.id AS signed_document_id, signed.uploaded_at AS signed_uploaded_at,
               coe.id AS coe_id, coe.coe_provider, coe.school_name, coe.coe_course,
               coe.coe_start_date, coe.coe_end_date, coe.file_path,
               document.id AS document_id, document.uploaded_at AS coe_uploaded_at
          FROM application_cycles cycle
          JOIN offer_received_events receipt ON receipt.application_cycle_id=cycle.id
               AND receipt.student_id=cycle.student_id
          JOIN offers offer ON offer.id=receipt.legacy_offer_id
               AND offer.application_cycle_id=cycle.id AND offer.student_id=cycle.student_id
          JOIN schools school ON school.id=receipt.school_id AND school.id=offer.school_id
          JOIN coe_oshc coe ON coe.application_cycle_id=cycle.id AND coe.student_id=cycle.student_id
          JOIN student_documents signed ON signed.id=(
               SELECT max(d.id) FROM student_documents d
                WHERE d.student_id=cycle.student_id AND d.file_path=offer.signed_offer_file_path
                  AND d.document_type='SignedOffer' AND d.is_active=1
                  AND d.document_status='active' AND d.voided_at IS NULL
          )
          JOIN student_documents document ON document.id=(
               SELECT max(d.id) FROM student_documents d
                WHERE d.student_id=cycle.student_id AND d.file_path=coe.file_path
                  AND d.document_type IN ('COE','COE_CAAW') AND d.is_active=1
                  AND d.document_status='active' AND d.voided_at IS NULL
          )
         WHERE cycle.student_id=? AND cycle.status='active' AND cycle.cycle_type='initial'
           AND cycle.education_stream='school_education'
           AND lower(offer.offer_status) IN ('signed','signed_received')
           AND upper(coe.document_kind) IN ('COE','COE_CAAW')
           AND lower(coalesce((SELECT state FROM offer_received_event_state_events
                WHERE offer_received_event_id=receipt.id ORDER BY occurred_at DESC,id DESC LIMIT 1
                ),'active'))='active'
           AND NOT EXISTS (SELECT 1 FROM cycle_final_acceptances WHERE application_cycle_id=cycle.id)
           AND NOT EXISTS (SELECT 1 FROM student_offer_acceptances
                            WHERE student_id=cycle.student_id AND offer_id<>offer.id)
    """, (student_id,))
    keys = [column[0] for column in cursor.description]
    candidates = []
    for values in cursor:
        row = dict(zip(keys, values))
        issued = business_date_iso(row["offer_date"])
        expiry = business_date_iso(row["offer_expiry_date"])
        signed = business_date_iso(row["signed_uploaded_at"])
        coe_uploaded = business_date_iso(row["coe_uploaded_at"])
        if (all((issued, expiry, signed, coe_uploaded))
                and issued <= signed <= coe_uploaded <= expiry < today.isoformat()
                and not eligibility_reason(row, today)):
            candidates.append(row)
    return candidates


def confirm_historical_school_coe(conn, student_id, offer_event_id, coe_id, actor, stamp):
    """Record staff confirmation of existing evidence within the caller's transaction."""
    from workflow_offer_events import select_final_acceptance

    matches = [row for row in historical_school_coe_rows(conn, student_id)
               if row["offer_event_id"] == offer_event_id and row["coe_id"] == coe_id]
    if len(matches) != 1:
        raise ValueError("找不到相符且在 Offer 有效期内上传的已签 Offer 和主课 CoE。")
    row = matches[0]
    acceptance = select_final_acceptance(
        conn, row["application_cycle_id"], offer_event_id, selected_at=stamp,
        actor_user_id=actor["id"], actor_name=actor["username"],
    )
    confirm_school_coe(conn, student_id, acceptance.id, coe_id, actor["id"], stamp)
    conn.execute("""
        INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
                               record_id,student_id,new_value,status,created_at)
        VALUES (?,?,'historical_final_school_coe_confirmed','coe_oshc','coe_oshc',?,?,?,'success',?)
    """, (actor["id"], actor["username"], coe_id, student_id, json.dumps({
        "application_cycle_id": row["application_cycle_id"], "acceptance_id": acceptance.id,
        "offer_event_id": offer_event_id, "signed_document_id": row["signed_document_id"],
        "signed_uploaded_at": row["signed_uploaded_at"], "coe_document_id": row["document_id"],
        "coe_uploaded_at": row["coe_uploaded_at"],
    }), stamp))
    return acceptance.id
