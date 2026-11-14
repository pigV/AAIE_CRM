"""Read the exact records behind the desktop dashboard metrics."""
from dashboard_business_time import register_dashboard_business_date


def _rows(conn, sql, parameters):
    cursor = conn.execute(sql, parameters)
    columns = tuple(column[0] for column in cursor.description)
    return [dict(zip(columns, row)) for row in cursor]


def _signing_records(conn, parameters):
    student_columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    contract_columns = {row[1] for row in conn.execute("PRAGMA table_info(contracts)")}
    current_student = "AND COALESCE(student.deleted_at,'')=''" if "deleted_at" in student_columns else ""
    current_contract = " ".join(
        f"AND COALESCE(contract.{column},'')=''"
        for column in ("deleted_at", "voided_at") if column in contract_columns
    )
    transfer_contract = (
        "OR contract.contract_type='TransferApplicationContract'"
        if "contract_type" in contract_columns else ""
    )
    title_column = "contract.contract_type" if "contract_type" in contract_columns else "NULL"
    sql = f"""
        SELECT contract.id AS record_id, student.student_id, contract.application_cycle_id,
               dashboard_business_date(contract.fully_executed_at) AS date,
               {title_column} AS title
        FROM contracts AS contract
        JOIN students AS student ON student.student_id=contract.student_id
        WHERE (contract.application_cycle_id IS NOT NULL {transfer_contract})
          AND contract.contract_status='FullyExecuted'
          AND dashboard_business_date(contract.fully_executed_at)>=?
          AND dashboard_business_date(contract.fully_executed_at)<?
          {current_student}
          {current_contract}
    """
    return _rows(conn, sql, parameters)


def _offer_records(conn, parameters):
    student_columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    has_offer_state_events = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='offer_received_event_state_events'"
    ).fetchone() is not None
    offer_state_clause = ""
    if has_offer_state_events:
        offer_state_clause = """
          AND COALESCE((
            SELECT state
            FROM offer_received_event_state_events AS state_event
            WHERE state_event.offer_received_event_id=offer_received_events.id
            ORDER BY occurred_at DESC,id DESC
            LIMIT 1
          ),'ACTIVE')<>'INVALID_RECORD'
        """
    current_offer_owner = (
        """AND NOT EXISTS (
               SELECT 1 FROM students AS student
                WHERE student.student_id=offer_received_events.student_id
                  AND COALESCE(student.deleted_at,'')<>''
           )""" if "deleted_at" in student_columns else ""
    )
    sql = f"""
        SELECT id AS record_id, student_id, application_cycle_id, school_name_snapshot AS title,
               legacy_offer_id, dashboard_business_date(received_at) AS date
        FROM offer_received_events
        WHERE dashboard_business_date(received_at)>=?
          AND dashboard_business_date(received_at)<?
          {offer_state_clause}
          {current_offer_owner}
    """
    return _rows(conn, sql, parameters)


def group_dashboard_metric_records(metric, records):
    ordered = sorted(records, key=lambda row: (row["date"], row["record_id"]), reverse=True)
    if metric == "offers":
        return [(row,) for row in ordered]
    grouped = {}
    for row in ordered:
        key = row["student_id"]
        details = grouped.setdefault(key, {})
        details.setdefault((row["record_id"], row["date"]), row)
    return [tuple(details.values()) for details in grouped.values()]


def load_dashboard_metric_rows(conn, metric, parameters):
    register_dashboard_business_date(conn)
    loader = {"signing": _signing_records, "offers": _offer_records}[metric]
    return group_dashboard_metric_records(metric, loader(conn, parameters))
