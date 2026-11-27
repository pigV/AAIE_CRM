"""Explicit, audited corrections of agreed client service fee receivables.

Signed contracts, original rule snapshots, payment allocations and receipts remain
immutable. This service has no HTTP entry point and never runs on page reads.
"""
from revenue.client_payments import ClientPaymentService
from revenue.historical_receipts import fee_kind


def preference_key(student_id):
    return f"revenue.student.{student_id}.hide_service_fee_balances"


def hide_service_fee_balances(connection, student_id):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_settings'").fetchone():
        return False
    row = connection.execute("SELECT value FROM system_settings WHERE key=?", (preference_key(student_id),)).fetchone()
    return bool(row and row[0] == "true")


def correct_service_fee(repository, *, fee_item_uid, student_id, expected_base_cents,
                        new_base_cents, hide_base_balances, actor_user_id, reason,
                        idempotency_key, preserve_issued_invoice=False):
    """Apply an approved amount once, rejecting stale or ambiguous fee authority."""
    if (type(new_base_cents) is not int or new_base_cents <= 0
            or type(expected_base_cents) is not int or expected_base_cents <= 0
            or type(hide_base_balances) is not bool or type(preserve_issued_invoice) is not bool
            or not str(reason).strip()):
        raise ValueError("Explicit positive amounts, display choice and reason required")
    c = repository.connection
    payload = dict(fee_item_uid=fee_item_uid, student_id=student_id,
                   expected_base_cents=expected_base_cents, new_base_cents=new_base_cents,
                   hide_base_balances=hide_base_balances, preserve_issued_invoice=preserve_issued_invoice, reason=reason)

    def operation():
        fee = ClientPaymentService(repository)._fee_context(fee_item_uid)
        if (fee['student_id'] != student_id or fee['net_cents'] != expected_base_cents
                or fee['gst_cents'] != 0 or fee['status'] not in ('unbilled','invoiced')
                or fee_kind(fee['milestone_code']) != 'client_service'):
            raise ValueError("Student, service fee amount or status changed; manual review required")
        # The immutable snapshot continues to identify the original signed contract.
        contract_id, cycle_id = ClientPaymentService._snapshot_contract_identity(fee)
        contract = c.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if (not contract or contract['student_id'] != student_id
                or contract['application_cycle_id'] != cycle_id or cycle_id != fee['application_cycle_id']
                or str(contract['contract_status']).replace('_','').replace(' ','').lower() != 'fullyexecuted'):
            raise ValueError("Original signed contract authority mismatch")
        invoices = c.execute("""SELECT DISTINCT i.uid,i.status,i.document_kind FROM revenue_invoice_lines l
            JOIN revenue_invoices i ON i.id=l.invoice_id
            WHERE l.source_kind='fee_item' AND l.source_uid=?""", (fee_item_uid,)).fetchall()
        if invoices and (not preserve_issued_invoice or any(
                i['status'] != 'issued' or i['document_kind'] != 'client_invoice' for i in invoices)):
            raise ValueError("Fee has an invoice; explicit preservation approval required")
        summary = ClientPaymentService(repository).fee_payment_summary(fee_item_uid)
        if summary['outstanding_base_cents'] is None or summary['base_received_cents'] > new_base_cents:
            raise ValueError("Unverified allocation or receipts exceed corrected fee; manual review required")
        before = dict(fee)
        c.execute("UPDATE revenue_fee_items SET net_cents=?,gross_cents=? WHERE id=?",
                  (new_base_cents, new_base_cents, fee['id']))
        if hide_base_balances:
            c.execute("""INSERT INTO system_settings(key,value,updated_at) VALUES(?,'true',?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                      (preference_key(student_id), repository.now()))
        return dict(uid=fee_item_uid, student_id=student_id, reason=reason,
                    before=before, after=dict(c.execute("SELECT * FROM revenue_fee_items WHERE id=?", (fee['id'],)).fetchone()),
                    hide_base_balances=hide_base_balances,
                    reduction_cents=expected_base_cents-new_base_cents,
                    preserved_invoice_uids=[i['uid'] for i in invoices])

    with repository.fee_write_transaction():
        return repository.command(idempotency_key=idempotency_key,
            command_type='client_service_fee_corrected', aggregate_type='fee_item',
            payload=payload, actor_user_id=actor_user_id, operation=operation)
