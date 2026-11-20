import json
import sqlite3

from application_cycle import close_cycle
from business_changes.withdrawals import synchronized_withdrawal


class ApplicationWithdrawalConflict(ValueError):
    pass


@synchronized_withdrawal
def withdraw_application(
    connection: sqlite3.Connection, *, student_id: str, cycle_id: int,
    actor_id: int, actor_name: str, reason: str, stage: str, timestamp: str,
) -> bool:
    """Withdraw one initial or progression application in the caller's transaction."""
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise ApplicationWithdrawalConflict("请填写终止原因，最多 1000 字。")
    cycle = connection.execute(
        """SELECT cycle.status, cycle.outcome, cycle.cycle_type, cycle.source_student_case_id
             FROM application_cycles AS cycle
             JOIN students AS student ON student.student_id=cycle.student_id
            WHERE cycle.id=? AND cycle.student_id=? AND cycle.cycle_type IN ('initial','progression')
              AND student.deleted_at IS NULL""",
        (cycle_id, student_id),
    ).fetchone()
    if cycle is None:
        raise ApplicationWithdrawalConflict("未找到可终止的本轮申请。")
    if cycle["status"] != "active":
        if cycle["outcome"] == "withdrawn" and connection.execute(
            """SELECT 1 FROM audit_logs WHERE student_id=? AND record_id=?
               AND module_name='application_cycles' AND action_type='application_withdrawn'
               AND status='success'""",
            (student_id, str(cycle_id)),
        ).fetchone():
            return False
        raise ApplicationWithdrawalConflict("本轮申请已经结案，请刷新页面。")

    if cycle["cycle_type"] == "progression":
        from student_lifecycle.adapters import ApplicationCycleGateway
        from student_lifecycle.commands import cancel_lifecycle

        cancel_lifecycle(
            connection, ApplicationCycleGateway(), case_id=cycle["source_student_case_id"],
            actor_user_id=actor_id, reason=reason, idempotency_key=f"withdrawal:{cycle_id}",
            occurred_at=timestamp, cycle_outcome="withdrawn",
        )
    else:
        close_cycle(connection, cycle_id, "withdrawn", timestamp, actor_id, actor_name)
    record_completed_withdrawal(
        connection, student_id=student_id, cycle_id=cycle_id, actor_id=actor_id,
        actor_name=actor_name, reason=reason, stage=stage, timestamp=timestamp,
    )
    return True


def record_completed_withdrawal(
    connection: sqlite3.Connection, *, student_id: str, cycle_id: int,
    actor_id: int, actor_name: str, reason: str, stage: str, timestamp: str,
) -> None:
    cycle = connection.execute(
        """SELECT parent_form_token FROM application_cycles
           WHERE id=? AND student_id=? AND status='closed' AND outcome='withdrawn'""",
        (cycle_id, student_id),
    ).fetchone()
    if cycle is None:
        raise ApplicationWithdrawalConflict("本轮申请未成功终止。")
    connection.execute(
        """UPDATE parent_form_tokens SET invalidated_at=?
           WHERE token=? AND submitted_at IS NULL AND invalidated_at IS NULL""",
        (timestamp, cycle["parent_form_token"]),
    )
    connection.execute(
        """UPDATE contract_signing_links SET status='revoked', invalidated_at=?,
                   invalidated_reason='application_withdrawn', updated_at=?
           WHERE status='active' AND signed_at IS NULL AND contract_id IN (
               SELECT id FROM contracts WHERE application_cycle_id=?
           )""",
        (timestamp, timestamp, cycle_id),
    )
    connection.execute(
        """UPDATE school_plan_report_links SET status='revoked', invalidated_at=?,
                   invalidated_reason='application_withdrawn'
           WHERE status='active' AND confirmed_at IS NULL AND report_id IN (
               SELECT id FROM school_plan_reports WHERE application_cycle_id=?
           )""",
        (timestamp, cycle_id),
    )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_information_collection_respondents'"
    ).fetchone():
        connection.execute(
            """UPDATE visa_information_collection_respondents
               SET token_status='Manually Revoked',invalidated_at=?,updated_at=?
               WHERE token_status='Active' AND invalidated_at IS NULL AND visa_case_id IN (
                   SELECT id FROM visa_cases WHERE application_cycle_id=?
               )""",
            (timestamp, timestamp, cycle_id),
        )
    connection.execute(
        """UPDATE reminders SET status='Cancelled', updated_by=?, updated_at=?
           WHERE student_id=? AND auto_generated=1
             AND status NOT IN ('Completed','Dismissed','Cancelled')
             AND (
                 (source_type='contract' AND related_record_id IN (
                     SELECT CAST(id AS TEXT) FROM contracts WHERE application_cycle_id=?
                 ))
                 OR (source_type='offer' AND related_record_id IN (
                     SELECT CAST(id AS TEXT) FROM offers WHERE application_cycle_id=?
                 ))
                 OR (source_type='transfer_v2' AND related_record_id=?)
             )""",
        (actor_id, timestamp, student_id, cycle_id, cycle_id, str(cycle_id)),
    )
    connection.execute(
        """INSERT INTO audit_logs(user_id, username, action_type, module_name, module_key,
               student_id, record_id, status, new_value, created_at)
           VALUES (?,?,'application_withdrawn','application_cycles','application_cycles',
                   ?,?,'success',?,?)""",
        (actor_id, actor_name, student_id, str(cycle_id),
         json.dumps({"reason": reason, "stage": stage}, ensure_ascii=False), timestamp),
    )


WITHDRAWAL_RECORDS = """
    FROM application_cycles AS cycle
    JOIN students AS student ON student.student_id=cycle.student_id
    JOIN audit_logs AS event ON event.id=(
        SELECT MAX(audit.id)
          FROM audit_logs AS audit
         WHERE audit.record_id=CAST(cycle.id AS TEXT)
           AND audit.student_id=cycle.student_id
           AND audit.module_name='application_cycles'
           AND audit.action_type='application_withdrawn'
           AND audit.status='success'
    )
    WHERE cycle.status='closed' AND cycle.outcome='withdrawn'
"""

REAPPLICATION_PREDECESSOR = (
    "EXISTS (SELECT 1 " + WITHDRAWAL_RECORDS
    + " AND cycle.student_id=fresh.student_id AND cycle.sequence_no<fresh.sequence_no)"
)


def count_withdrawn_students(connection: sqlite3.Connection) -> int:
    return connection.execute(
        "SELECT COUNT(DISTINCT cycle.student_id) " + WITHDRAWAL_RECORDS
    ).fetchone()[0]


def can_start_reapplication(connection: sqlite3.Connection, student_id: str, cycle_id: int) -> bool:
    return connection.execute(
        "SELECT 1 " + WITHDRAWAL_RECORDS + """
          AND cycle.id=? AND cycle.student_id=?
          AND NOT EXISTS (
              SELECT 1 FROM application_cycles AS newer
               WHERE newer.student_id=cycle.student_id
                 AND (newer.status='active' OR newer.sequence_no>cycle.sequence_no)
          )
        """,
        (cycle_id, student_id),
    ).fetchone() is not None


def reapplication_cycle(connection: sqlite3.Connection, token: str, student_id: str) -> int | None:
    row = connection.execute(
        """
        SELECT fresh.id
          FROM application_cycles AS fresh
         WHERE fresh.parent_form_token=? AND fresh.student_id=?
           AND fresh.status='active' AND fresh.cycle_type='initial'
           AND
        """ + REAPPLICATION_PREDECESSOR,
        (token, student_id),
    ).fetchone()
    return int(row[0]) if row else None


def list_withdrawn_applications(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT cycle.id AS cycle_id, cycle.student_id, cycle.sequence_no,
               cycle.cycle_type, cycle.closed_at,
               student.passport_first_name, student.passport_last_name,
               event.username AS operator, event.new_value AS details
        """
        + WITHDRAWAL_RECORDS
        + " ORDER BY cycle.closed_at DESC, cycle.id DESC"
    )
    records = []
    for row in rows:
        record = dict(row)
        details = json.loads(record.pop("details"))
        record.update(reason=details["reason"], stage=details["stage"])
        records.append(record)
    return records
