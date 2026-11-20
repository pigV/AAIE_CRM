"""End a transfer intake that has not opened an application cycle."""

from student_lifecycle.transfer_state import PENDING_TRANSFER_WITHDRAWN, load_student_transfer_state_map


class PendingTransferWithdrawalConflict(ValueError):
    pass


def pending_transfer_contract(connection, student_id, contract_id, *, verify_current=True):
    if verify_current:
        state = load_student_transfer_state_map(connection, (student_id,)).get(student_id)
        if not state or state.state != "contract_pending" or state.contract_id != contract_id:
            raise PendingTransferWithdrawalConflict("该合同已不属于当前待签转学申请。")
    contract = connection.execute(
        """SELECT id,contract_status,contract_title,fully_executed_at,final_signed_file_path FROM contracts
            WHERE id=? AND student_id=? AND contract_type='TransferApplicationContract'
              AND deleted_at IS NULL AND voided_at IS NULL AND application_cycle_id IS NULL""",
        (contract_id, student_id),
    ).fetchone()
    if contract is None:
        raise PendingTransferWithdrawalConflict("未找到可终止的待签转学申请。")
    signed = connection.execute(
        """SELECT 1 FROM contract_signatures WHERE contract_id=? AND status='signed'
           UNION ALL SELECT 1 FROM contract_signing_links WHERE contract_id=? AND status='signed'
           UNION ALL SELECT 1 FROM contracts WHERE id=?
             AND (coalesce(signed_at,'')<>'' OR coalesce(fully_executed_at,'')<>''
                  OR coalesce(signed_file_path,'')<>'' OR coalesce(final_signed_file_path,'')<>'')
           LIMIT 1""",
        (contract_id, contract_id, contract_id),
    ).fetchone()
    if (contract["fully_executed_at"] or contract["final_signed_file_path"]
            or contract["contract_status"] not in {"Draft", "Active", "Sent", "Expired", "ParentSigned"}):
        raise PendingTransferWithdrawalConflict("这份合同已完成签署或状态已变更，请先核对合同。")
    return {**dict(contract), "has_signature": bool(signed or contract["contract_status"] == "ParentSigned")}


def _withdraw_pending_transfer(connection, student_id, contract_id, reason, *, actor, timestamp, keep_signature=False):
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise PendingTransferWithdrawalConflict("请填写终止原因，最多 1000 字。")
    contract = pending_transfer_contract(connection, student_id, contract_id)
    if contract["has_signature"] != keep_signature:
        raise PendingTransferWithdrawalConflict("合同签署状态已变更，请重新打开确认页。")
    if not keep_signature:
        connection.execute(
            "UPDATE contracts SET contract_status='Revoked',updated_at=? WHERE id=?",
            (timestamp, contract_id),
        )
    connection.execute(
        """UPDATE contract_signing_links SET status='revoked',invalidated_at=?,
                 invalidated_reason='pending_transfer_withdrawn',updated_at=?
            WHERE contract_id=? AND status='active'""",
        (timestamp, timestamp, contract_id),
    )
    connection.execute(
        """INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
                                   record_id,student_id,contract_id,old_value,new_value,status,created_at)
           VALUES (?,?,?,'contracts','contracts',?,?,?,?,?,'success',?)""",
        (actor["id"], actor["username"], PENDING_TRANSFER_WITHDRAWN, str(contract_id), student_id, str(contract_id),
         contract["contract_status"], reason, timestamp),
    )


def withdraw_pending_transfer(connection, student_id, contract_id, reason, *, actor, timestamp, keep_signature=False):
    """Retain the production mutation inside the candidate change boundary."""
    from business_changes.commands import Mutation
    from business_changes.contracts import Subject
    from business_changes.domain import run_domain_write
    from business_changes.student_records import student_impacts

    student_id, contract_id = str(student_id), int(contract_id)

    def capture(view):
        return (
            tuple(tuple(row) for row in view.execute("SELECT * FROM contracts WHERE id=?", (contract_id,))),
            tuple(tuple(row) for row in view.execute("SELECT * FROM contract_signing_links WHERE contract_id=? ORDER BY id", (contract_id,))),
            tuple(tuple(row) for row in view.execute("SELECT id FROM audit_logs WHERE action_type=? AND contract_id=? ORDER BY id", (PENDING_TRANSFER_WITHDRAWN, str(contract_id)))),
        )

    def describe(view, result, before, after):
        links = tuple(Subject("contract_link", str(row[0])) for row in view.execute(
            "SELECT id FROM contract_signing_links WHERE contract_id=?", (contract_id,)))
        return Mutation("contract.pending_transfer_withdrawn", Subject("contract", str(contract_id)),
                        (Subject("student", student_id), Subject("module", "reminders"),
                         *student_impacts(view, student_id), *links))

    return run_domain_write(connection, event_type="contract.pending_transfer_withdrawn",
        actor_kind="user", actor_ref=str(actor["id"]), capture=capture,
        operation=lambda view: _withdraw_pending_transfer(view, student_id, contract_id, reason,
            actor=actor, timestamp=timestamp, keep_signature=keep_signature), describe=describe)
