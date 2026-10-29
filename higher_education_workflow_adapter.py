"""Read-only Workflow V2 facts for a higher-education Application Cycle."""


def _exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _count(conn, sql, params):
    return int(conn.execute(sql, params).fetchone()[0])


def _cycle(conn, cycle_id):
    row = conn.execute(
        "SELECT id,education_stream,status FROM application_cycles WHERE id=?", (cycle_id,)
    ).fetchone()
    if row is None:
        raise ValueError("application cycle does not exist")
    return row


def contract_applicable(conn, cycle_id):
    return str(_cycle(conn, cycle_id)[1]) != "higher_education"


def student_higher_education_experience(conn, student_id):
    """Resolve student-page behavior without changing either lifecycle."""
    if not _exists(conn, "application_cycles"):
        return {
            "higher_education_active": False,
            "school_education_active": False,
            "contract_applicable": True,
            "k12_report_applicable": True,
        }
    rows = conn.execute(
        """SELECT education_stream FROM application_cycles
           WHERE student_id=? AND status='active'""",
        (student_id,),
    ).fetchall()
    streams = {str(row[0]) for row in rows}
    higher_education_active = "higher_education" in streams
    school_education_active = "school_education" in streams
    return {
        "higher_education_active": higher_education_active,
        "school_education_active": school_education_active,
        "contract_applicable": not higher_education_active or school_education_active,
        "k12_report_applicable": not higher_education_active or school_education_active,
    }


def higher_education_facts(conn, cycle_id):
    cycle = _cycle(conn, cycle_id)
    if str(cycle[1]) != "higher_education":
        raise ValueError("higher education facts require a higher education cycle")
    plan_confirmed = _count(
        conn,
        "SELECT count(*) FROM higher_education_plans WHERE application_cycle_id=? AND status IN ('Confirmed','Converted')",
        (cycle_id,),
    ) > 0
    submitted = _count(
        conn,
        "SELECT count(*) FROM higher_education_applications WHERE application_cycle_id=? AND status NOT IN ('Draft','Withdrawn')",
        (cycle_id,),
    )
    offer_received = _count(
        conn,
        "SELECT count(*) FROM higher_education_offers WHERE application_cycle_id=?",
        (cycle_id,),
    ) > 0
    offer_accepted = _count(
        conn,
        "SELECT count(*) FROM higher_education_offers WHERE application_cycle_id=? AND status='Accepted'",
        (cycle_id,),
    ) > 0
    coe_issued = (
        _count(conn, "SELECT count(*) FROM coe_oshc WHERE application_cycle_id=?", (cycle_id,)) > 0
        if _exists(conn, "coe_oshc") else False
    )
    visa_recorded = (
        _count(conn, "SELECT count(*) FROM visa_cases WHERE application_cycle_id=?", (cycle_id,)) > 0
        if _exists(conn, "visa_cases") else False
    )
    temporary_share_pending = _count(
        conn,
        """SELECT count(*) FROM higher_education_temporary_commission_shares s
           JOIN higher_education_applications a ON a.id=s.application_id
           WHERE a.application_cycle_id=? AND s.status='Pending'""",
        (cycle_id,),
    ) > 0
    commission_line_count = _count(
        conn,
        """SELECT count(*) FROM higher_education_commission_lines l
           JOIN higher_education_applications a ON a.id=l.application_id
           WHERE a.application_cycle_id=? AND l.status<>'Void'""",
        (cycle_id,),
    )
    commission_claimable = _count(
        conn,
        """SELECT count(*) FROM higher_education_commission_lines l
           JOIN higher_education_applications a ON a.id=l.application_id
           WHERE a.application_cycle_id=? AND l.status IN ('Claimable','Invoiced','Paid')""",
        (cycle_id,),
    ) > 0
    return {
        "education_stream": "higher_education",
        "cycle_active": str(cycle[2]) == "active",
        "contract_applicable": False,
        "plan_confirmed": plan_confirmed,
        "submitted_applications": submitted,
        "offer_received": offer_received,
        "offer_accepted": offer_accepted,
        "coe_issued": coe_issued,
        "visa_recorded": visa_recorded,
        "temporary_share_pending": temporary_share_pending,
        "commission_line_count": commission_line_count,
        "commission_claimable": commission_claimable,
    }
