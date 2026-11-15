"""Staff confirmation of a new preparation round after a terminal Visa result."""

import json


def retry_key(case):
    result = str(case.get("effective_result") or case.get("status") or "").strip().lower()
    if result not in {"refused", "withdrawn", "cancelled", "canceled"}:
        return None
    return json.dumps([result, case.get("source_event_id"), case.get("result_date")])


def load_retries(connection, cycle_ids):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_logs'").fetchone()
    if not exists or not cycle_ids:
        return {}
    marks = ",".join("?" for _ in cycle_ids)
    retries = {}
    for case_id, student_id, result_key in connection.execute(f"""
        SELECT visa.id,visa.student_id,audit.old_value FROM visa_cases visa
          JOIN audit_logs audit ON audit.record_id=CAST(visa.id AS TEXT)
               AND audit.student_id=visa.student_id AND audit.module_name='visa_cases'
               AND audit.action_type='student_visa_preparation_resumed' AND audit.status='success'
         WHERE visa.application_cycle_id IN ({marks})
         ORDER BY audit.id DESC
    """, cycle_ids):
        retries.setdefault((case_id, str(student_id)), result_key)
    return retries
