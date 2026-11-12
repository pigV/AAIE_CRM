"""Read the school enrolment, its Offer files and its current first-year tuition."""

from revenue.operational_visibility import RevenueOperationalVisibility

OFFER_KIND = "first_year_tuition_offer"
SCHEDULE_KIND = "first_year_tuition_schedule"
COE_KIND = "first_year_tuition_coe"
EVIDENCE_KINDS = (OFFER_KIND, SCHEDULE_KIND, COE_KIND)
OFFER_EXTENSIONS = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".txt"}


def school_components(connection, case_uid):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(cycle_final_acceptances)")}
    if not {"school_id", "student_id", "acceptance_status"} <= columns:
        return []
    visibility = RevenueOperationalVisibility(connection)
    return [dict(row) for row in connection.execute(
        """SELECT ec.id, ec.uid, c.student_id, c.application_cycle_id, a.school_id,
                  s.school_name
             FROM revenue_enrolment_components ec
             JOIN revenue_commercial_cases c ON c.id=ec.commercial_case_id
             JOIN cycle_final_acceptances a ON a.id=ec.final_acceptance_id
             JOIN schools s ON s.id=a.school_id
            WHERE c.uid=? AND ec.component_kind='school' AND ec.status='active'
              AND a.acceptance_status='active' AND a.student_id=c.student_id
              AND a.application_cycle_id=c.application_cycle_id AND """
        + visibility.case_predicate("c") + " ORDER BY ec.id", (case_uid,),
    )]


def component_offers(connection, component):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(offer_received_events)")}
    if not {"legacy_offer_id", "school_id", "student_id"} <= columns:
        return []
    return [dict(row) for row in connection.execute(
        """SELECT DISTINCT o.*
             FROM offer_received_events e JOIN offers o ON o.id=e.legacy_offer_id
            WHERE e.application_cycle_id=? AND e.school_id=? AND e.student_id=?
              AND o.student_id=e.student_id AND o.school_id=e.school_id
              AND COALESCE(o.offer_status,'') NOT IN ('cancelled','rejected','expired','withdrawn')
              AND (COALESCE(o.offer_file_path,'')<>'' OR COALESCE(o.signed_offer_file_path,'')<>'')
            ORDER BY o.id DESC""",
        (component["application_cycle_id"], component["school_id"], component["student_id"]),
    )]


def first_year_records(connection, component_id):
    return [dict(row) for row in connection.execute(
        """SELECT t.*, d.uid AS document_uid, d.document_kind,
                  (SELECT json_extract(a.after_json,'$.source_label')
                     FROM revenue_audit_events a WHERE a.aggregate_uid=t.uid
                      AND a.action IN ('first_year_tuition_saved','student_intake_tuition_linked','first_year_tuition_amount_corrected')
                      AND json_extract(a.after_json,'$.source_label') IS NOT NULL
                    ORDER BY a.id DESC LIMIT 1) AS source_label,
                  (SELECT json_extract(a.after_json,'$.intake_year') FROM revenue_audit_events a
                     WHERE a.aggregate_uid=t.uid AND a.action IN ('student_intake_tuition_linked','first_year_tuition_amount_corrected')
                       AND json_extract(a.after_json,'$.intake_year') IS NOT NULL
                     ORDER BY a.id DESC LIMIT 1) AS intake_year,
                  (SELECT json_extract(a.after_json,'$.intake_grade') FROM revenue_audit_events a
                     WHERE a.aggregate_uid=t.uid AND a.action IN ('student_intake_tuition_linked','first_year_tuition_amount_corrected')
                       AND json_extract(a.after_json,'$.intake_grade') IS NOT NULL
                     ORDER BY a.id DESC LIMIT 1) AS intake_grade
             FROM revenue_tuition_facts t
             LEFT JOIN revenue_documents d ON d.id=t.evidence_document_id
            WHERE t.enrolment_component_id=? AND t.status='active'
              AND lower(trim(t.academic_period)) IN ('year_1','first_year')
              AND t.fee_component IN ('tuition','tuition_local','tuition_international')
            ORDER BY t.id""", (component_id,),
    )]


def can_read_evidence(connection, document_id):
    visibility = RevenueOperationalVisibility(connection)
    return connection.execute(
        """SELECT 1 FROM revenue_tuition_facts t
            JOIN revenue_enrolment_components ec ON ec.id=t.enrolment_component_id
            JOIN revenue_commercial_cases c ON c.id=ec.commercial_case_id
           WHERE t.evidence_document_id=? AND """ + visibility.case_predicate("c") + " LIMIT 1",
        (document_id,),
    ).fetchone() is not None
