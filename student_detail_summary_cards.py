"""Presentation-free summary data for the student detail cards."""

from __future__ import annotations

from dataclasses import dataclass

from student_application_stage_distribution import load_application_stage_result_map
from student_followup_notes import StudentFollowupNote, get_next_student_followup_note
from workflow_projection import load_current_workflow_projection
from workflow_cutover import is_workflow_v2_active


@dataclass(frozen=True)
class SchoolPlanCardSummary:
    names: tuple[str, ...]
    remaining_count: int = 0
    is_final: bool = False
    offer_received_names: tuple[str, ...] = ()
    waiting_result_names: tuple[str, ...] = ()
    not_submitted_names: tuple[str, ...] = ()
    final_school_name: str = ""
    submitted_count: int = 0
    has_application_cycle: bool | None = None
    application_cycle_id: int | None = None
    empty_label: str = ""


@dataclass(frozen=True)
class StudentDetailCardSummary:
    student_id: str
    stage_key: str
    stage_label: str
    created_date: str
    schools: SchoolPlanCardSummary
    next_reminder: StudentFollowupNote | None = None


def has_current_school_workflow(projection, schools: SchoolPlanCardSummary) -> bool:
    """Current, cycle-owned school work outranks a stale profile service label."""
    return bool(
        getattr(projection, "state", None) == "active"
        and getattr(projection, "cycle_id", None) is not None
        and getattr(schools, "application_cycle_id", None) == projection.cycle_id
        and schools.has_application_cycle is True
        and (schools.names or schools.is_final or schools.submitted_count)
    )


def load_student_detail_workflow_projection(connection, student_id: str):
    """Thin Student Detail adapter over the authoritative cycle projection."""
    target = str(student_id)
    cycle_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM application_cycles WHERE status='active' AND student_id=?",
            (target,),
        ).fetchall()
    )
    return tuple(
        row
        for row in load_current_workflow_projection(connection, cycle_ids=cycle_ids)
        if row.student_id == target
    )


def _canonical_school_submissions(connection,cycle_id,student_id,confirmed_ids):
    """Current-cycle formal commands take precedence over mirrored old flags.

    Legacy-only rows remain readable, but an explicit canonical retraction must
    never be revived by a stale legacy `submitted` value. No source is rewritten.
    """
    if not confirmed_ids:return {}
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('private_school_applications','public_application_groups','public_application_preferences')")}
    result={}
    if 'private_school_applications' in tables:
        for row in connection.execute('''SELECT confirmed_school_id,status,submitted_at
            FROM private_school_applications WHERE application_cycle_id=? AND student_id=?''',(cycle_id,student_id)):
            if row[0] in confirmed_ids:result[row[0]]=row[1]!='not_submitted' and bool(row[2])
    if {'public_application_groups','public_application_preferences'}<=tables:
        for row in connection.execute('''SELECT preference.confirmed_school_id,application.status,application.submitted_at
            FROM public_application_groups application JOIN public_application_preferences preference
                ON preference.public_application_group_id=application.id
            WHERE application.application_cycle_id=? AND application.student_id=?''',(cycle_id,student_id)):
            if row[0] in confirmed_ids:result[row[0]]=row[1]!='draft' and bool(row[2])
    return result


def _load_higher_education_school_summary(connection, student_id, cycle_id):
    """University work owns its own schools; K-12 plans remain historical."""
    names, offers, waiting, drafts = {}, {}, {}, {}
    applications = {}
    submitted_count = 0
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def receipt_guard(source, alias):
        if not {'offer_received_events','offer_received_event_state_events'} <= tables:
            return ''
        fields = {row[1] for row in connection.execute('PRAGMA table_info(offer_received_events)')}
        if source not in fields:
            return ''
        return f'''AND NOT EXISTS (SELECT 1 FROM offer_received_events receipt
            WHERE receipt.{source}={alias}.id AND receipt.application_cycle_id={alias}.application_cycle_id
              AND lower(coalesce((SELECT state.state FROM offer_received_event_state_events state
                  WHERE state.offer_received_event_id=receipt.id
                  ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1),'active'))<>'active')'''
    if {'higher_education_applications', 'higher_education_providers'} <= tables:
        for application_id, name, status, submitted_at in connection.execute('''
            SELECT application.id,provider.provider_name,application.status,application.submitted_at
            FROM higher_education_applications application
            JOIN higher_education_providers provider ON provider.id=application.provider_id
            WHERE application.application_cycle_id=? AND application.is_current=1
              AND lower(application.status) NOT IN ('withdrawn','superseded','closed')
            ORDER BY application.id''', (cycle_id,)):
            name = ' '.join(str(name or '').split())
            key = _school_key(name)
            if not key:
                continue
            names.setdefault(key, name)
            applications[application_id] = (key, name)
            if submitted_at and str(status).casefold() != 'draft':
                submitted_count += 1
                waiting.setdefault(key, name)
            else:
                drafts.setdefault(key, name)
        if applications and 'higher_education_offers' in tables:
            for application_id, in connection.execute(f'''
                SELECT offer.application_id FROM higher_education_offers offer
                WHERE offer.application_cycle_id=? AND lower(offer.status) IN ('received','accepted')
                  {receipt_guard('higher_education_offer_id','offer')}''', (cycle_id,)):
                if application_id in applications:
                    key, name = applications[application_id]
                    offers.setdefault(key, name)
    if 'offers' in tables:
        document_guard = '''AND NOT EXISTS (SELECT 1 FROM student_documents document
            WHERE document.student_id=offer.student_id AND document.file_path=offer.offer_file_path
              AND (document.is_active=0 OR document.document_status IN ('voided','deleted','archived')
                   OR document.voided_at IS NOT NULL))''' if 'student_documents' in tables else ''
        from dashboard_business_time import business_date_iso
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo('Australia/Melbourne')).date().isoformat()
        for name, expiry, status, kind in connection.execute(f'''
            SELECT offer.school_name,offer.offer_expiry_date,offer.offer_status,offer.document_type FROM offers offer
            WHERE offer.student_id=? AND offer.application_cycle_id=?
              AND offer.document_type IN ('Offer','SignedOffer')
              AND trim(coalesce(offer.offer_file_path,''))<>''
              AND lower(coalesce(offer.offer_status,'')) NOT IN
                  ('void','voided','cancelled','canceled','withdrawn','declined','rejected','superseded','invalid_record')
              {document_guard} {receipt_guard('legacy_offer_id','offer')}
              ORDER BY offer.id''', (student_id, cycle_id)):
            expiry_date = business_date_iso(expiry)
            expired = bool(expiry and (not expiry_date or expiry_date < today))
            signed_history = str(status or '').casefold() in {'accepted','signed','signed_received'} or kind == 'SignedOffer'
            if expired and not signed_history:
                continue
            name = ' '.join(str(name or '').split())
            key = _school_key(name)
            if key:
                names.setdefault(key, name)
                if not expired:
                    offers.setdefault(key, name)
    from canonical_acceptance import active_acceptance_predicate
    accepted = connection.execute(f'''SELECT school_name_snapshot FROM cycle_final_acceptances
        WHERE application_cycle_id=? AND {active_acceptance_predicate(connection)}''', (cycle_id,)).fetchone()
    final_name = str(accepted[0] or '').strip() if accepted else ''
    # A stale K-12 final acceptance is not a current university school.
    is_final = bool(final_name and _school_key(final_name) in names)
    return SchoolPlanCardSummary(
        (final_name,) if is_final else tuple(names.values())[:3],
        0 if is_final else max(len(names)-3, 0), is_final=is_final,
        offer_received_names=tuple(offers.values()),
        waiting_result_names=tuple(name for key,name in waiting.items() if key not in offers),
        not_submitted_names=tuple(name for key,name in drafts.items() if key not in offers),
        final_school_name=final_name if is_final else '', submitted_count=submitted_count,
        has_application_cycle=True, application_cycle_id=cycle_id,
        empty_label='暂无当前大学学校方案/申请',
    )


def _load_school_plan_card_summary(connection, student_id: str) -> SchoolPlanCardSummary:
    if is_workflow_v2_active(connection):
        fields = {row[1] for row in connection.execute('PRAGMA table_info(application_cycles)')}
        stream = 'education_stream' if 'education_stream' in fields else "'school_education'"
        cycle = connection.execute(
            f"""SELECT id,{stream} FROM application_cycles WHERE student_id=?
                 ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                          sequence_no DESC,id DESC LIMIT 1""",
            (student_id,),
        ).fetchone()
        if cycle is None:
            return SchoolPlanCardSummary((), has_application_cycle=False)
        cycle_id = int(cycle[0])
        if str(cycle[1]) == 'higher_education':
            return _load_higher_education_school_summary(connection, student_id, cycle_id)
        from canonical_acceptance import active_acceptance_predicate

        active_acceptance = active_acceptance_predicate(connection)
        accepted = connection.execute(
            f"""SELECT school_name_snapshot FROM cycle_final_acceptances
                 WHERE application_cycle_id=? AND {active_acceptance}""",
            (cycle_id,),
        ).fetchone()
        rows = connection.execute(
            """SELECT cs.id,cs.school_name_snapshot,
                      COALESCE(cs.submission_status,'not_submitted'),
                      COALESCE(cs.result_status,'pending'),cs.result_offer_id
                 FROM school_plan_confirmed_schools cs
                WHERE cs.report_id IN (
                      SELECT report.id
                        FROM school_plan_reports report
                             INDEXED BY idx_school_plan_reports_cycle
                       WHERE report.application_cycle_id=?
                         AND report.cycle_is_current=1
                )
                  AND cs.is_selected=1 AND cs.student_id=?
                ORDER BY cs.report_id,cs.id""",
            (cycle_id,student_id),
        ).fetchall()
        formal=_canonical_school_submissions(connection,cycle_id,student_id,{row[0] for row in rows})
        submitted=lambda row:formal.get(row[0],str(row[2])=='submitted')
        names, offers, waiting, not_submitted = [], [], [], []
        seen = set()
        for row in rows:
            name = " ".join(str(row[1] or "").strip().split())
            key = _school_key(name)
            if key and key not in seen:
                seen.add(key)
                names.append(name)
                if not submitted(row):
                    not_submitted.append(name)
                elif str(row[3]) == "offer_received" and row[4] is not None:
                    from application_cycle import list_matching_offers_for_confirmed_school

                    valid_ids = {
                        candidate.id
                        for candidate in list_matching_offers_for_confirmed_school(
                            connection, int(row[0])
                        )
                    }
                    if int(row[4]) in valid_ids:
                        offers.append(name)
                elif str(row[3]) == "pending":
                    waiting.append(name)
        submitted_count = sum(submitted(row) for row in rows)
        if accepted is not None:
            name = " ".join(str(accepted[0] or "").strip().split())
            return SchoolPlanCardSummary((name,) if name else (), is_final=True,
                final_school_name=name, submitted_count=submitted_count,
                has_application_cycle=True, application_cycle_id=cycle_id)
        return SchoolPlanCardSummary(tuple(names[:3]), max(len(names) - 3, 0),
            offer_received_names=tuple(offers), waiting_result_names=tuple(waiting),
            not_submitted_names=tuple(not_submitted), submitted_count=submitted_count,
            has_application_cycle=True, application_cycle_id=cycle_id)

    accepted_offer = connection.execute(
        """SELECT o.school_name
              FROM student_offer_acceptances acceptance
              JOIN offers o
                ON o.id=acceptance.offer_id AND o.student_id=acceptance.student_id
             WHERE acceptance.student_id=?""",
        (student_id,),
    ).fetchone()
    if accepted_offer is not None:
        name = " ".join(str(accepted_offer["school_name"] or "").strip().split())
        return SchoolPlanCardSummary((name,) if name else (), is_final=True,
            final_school_name=name, has_application_cycle=None)

    rows = connection.execute(
        """SELECT cs.school_name_snapshot,
                  COALESCE(cs.submission_status,'not_submitted') submission_status,
                  COALESCE(cs.result_status,'pending') result_status
              FROM school_plan_reports r
              JOIN school_plan_confirmed_schools cs
                ON cs.report_id=r.id
               AND cs.student_id=r.student_id
               AND cs.student_id=?
             WHERE r.student_id=?
               AND r.is_current=1
               AND r.invalidated_at IS NULL
               AND cs.is_selected=1
               AND (
                    COALESCE(r.confirmed_at,'')<>''
                    OR lower(COALESCE(r.status,''))='confirmed'
                    OR EXISTS(
                        SELECT 1
                          FROM school_plan_report_links link
                         WHERE link.report_id=r.id
                           AND link.status='active'
                           AND link.invalidated_at IS NULL
                           AND COALESCE(link.confirmed_at,'')<>''
                    )
               )
             ORDER BY r.id,
                      CASE WHEN cs.rank_order IS NULL THEN 1 ELSE 0 END,
                      cs.rank_order,
                      cs.id""",
        (student_id, student_id),
    ).fetchall()
    names, offers, waiting, not_submitted = [], [], [], []
    seen = set()
    for row in rows:
        name = " ".join(str(row["school_name_snapshot"] or "").strip().split())
        key = _school_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if str(row["submission_status"]) != "submitted":
            not_submitted.append(name)
        elif str(row["result_status"]) == "offer_received":
            offers.append(name)
        elif str(row["result_status"]) == "pending":
            waiting.append(name)
    return SchoolPlanCardSummary(tuple(names[:3]), max(len(names) - 3, 0),
        offer_received_names=tuple(offers), waiting_result_names=tuple(waiting),
        not_submitted_names=tuple(not_submitted),
        submitted_count=sum(str(row["submission_status"]) == "submitted" for row in rows))


def _school_key(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def load_student_detail_card_summary(
    connection,
    student_id: str,
    *,
    workflow_projection_map=None,
) -> StudentDetailCardSummary:
    student_id = str(student_id)
    student = connection.execute(
        "SELECT student_id,created_date FROM students WHERE student_id=? AND deleted_at IS NULL",
        (student_id,),
    ).fetchone()
    if student is None:
        raise LookupError("学生记录不存在。")
    stage = load_application_stage_result_map(
        connection,
        (student_id,),
        workflow_projection_map=workflow_projection_map,
    )[student_id]
    return StudentDetailCardSummary(
        student_id=student_id,
        stage_key=str(stage.key or "excluded"),
        stage_label=str(stage.label or stage.excluded_reason or "阶段未记录"),
        created_date=str(student["created_date"] or "")[:10],
        schools=_load_school_plan_card_summary(connection, student_id),
        next_reminder=get_next_student_followup_note(connection, student_id),
    )
