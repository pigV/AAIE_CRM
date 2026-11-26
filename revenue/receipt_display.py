"""Original-currency receipt projection; never rewrites financial records."""
from collections import defaultdict


def original_currency_receipts(connection, fee_ids):
    requested = set(fee_ids)
    if not requested:
        return {}
    marks = ','.join('?' for _ in requested)
    # Include every allocation of each matching payment, including hidden fees,
    # so a shared historical receipt is never counted in full on multiple rows.
    legacy = legacy_receipt_links(connection)
    links = '''SELECT payment_id,fee_item_id,SUM(gross_charge_aud_cents) weight
                 FROM revenue_client_payment_allocations GROUP BY payment_id,fee_item_id'''
    if legacy:
        links += ' UNION ALL ' + legacy
    rows = connection.execute(
        f"""WITH links AS ({links}) SELECT p.id,p.currency_code,p.amount_cents,a.fee_item_id,
                   SUM(a.weight) weight
              FROM revenue_payments p
              JOIN links a ON a.payment_id=p.id
             WHERE p.status='confirmed' AND p.payment_direction='received'
               AND p.counterparty_kind='client'
               AND EXISTS(SELECT 1 FROM links target
                           WHERE target.payment_id=p.id AND target.fee_item_id IN ({marks}))
             GROUP BY p.id,a.fee_item_id ORDER BY p.id,a.fee_item_id""", tuple(requested)
    ).fetchall()
    payments = defaultdict(list)
    for row in rows:
        payments[row['id']].append(row)
    totals = defaultdict(lambda: defaultdict(int))
    for allocations in payments.values():
        amount = int(allocations[0]['amount_cents'])
        total_weight = sum(int(a['weight']) for a in allocations)
        if total_weight <= 0:
            continue
        # Legacy AUD allocations are already in the actual currency. Preserve
        # unallocated cash instead of stretching known allocations to the receipt.
        denominator = max(amount, total_weight) if allocations[0]['currency_code'] == 'AUD' else total_weight
        shares = [amount * int(a['weight']) // denominator for a in allocations]
        attributed = amount * total_weight // denominator
        remainder_order = sorted(range(len(allocations)), key=lambda i: (
            -(amount * int(allocations[i]['weight']) % denominator), allocations[i]['fee_item_id'] if allocations[i]['fee_item_id'] is not None else -1))
        for i in remainder_order[:attributed - sum(shares)]:
            shares[i] += 1
        for allocation, share in zip(allocations, shares):
            fee_id = allocation['fee_item_id']
            if fee_id in requested:
                totals[fee_id][allocation['currency_code']] += share
    return {fee_id: [{'currency_code': currency, 'amount_cents': amount}
                     for currency, amount in sorted(by_currency.items())]
            for fee_id, by_currency in totals.items()}


def legacy_receipt_links(connection):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'revenue_invoice_lines','revenue_payment_allocations','revenue_fee_items','revenue_invoices'} <= tables:
        return ''
    return """SELECT a.payment_id,f.id fee_item_id,SUM(a.allocated_cents) weight
                FROM revenue_payment_allocations a
                JOIN revenue_invoice_lines il ON il.id=a.invoice_line_id AND il.source_kind='fee_item'
                JOIN revenue_fee_items f ON f.uid=il.source_uid AND f.student_id=il.student_id
                JOIN revenue_invoices i ON i.id=il.invoice_id AND i.commercial_case_id=f.commercial_case_id
                  AND i.status='issued' AND i.document_kind='client_invoice'
               WHERE NOT EXISTS(SELECT 1 FROM revenue_client_payment_allocations ca
                                 WHERE ca.payment_id=a.payment_id AND ca.fee_item_id=f.id)
               GROUP BY a.payment_id,f.id
               UNION ALL
               SELECT a.payment_id,NULL fee_item_id,SUM(a.allocated_cents) weight
                 FROM revenue_payment_allocations a
                WHERE NOT EXISTS(
                    SELECT 1 FROM revenue_invoice_lines il
                    JOIN revenue_fee_items f ON il.source_kind='fee_item' AND il.source_uid=f.uid AND il.student_id=f.student_id
                    JOIN revenue_invoices i ON i.id=il.invoice_id AND i.commercial_case_id=f.commercial_case_id
                     AND i.status='issued' AND i.document_kind='client_invoice'
                    WHERE il.id=a.invoice_line_id)
                GROUP BY a.payment_id"""


def legacy_fee_receipt_totals(connection, case_uid):
    """Use known invoice allocations; partial GST remains explicitly unresolved."""
    if not legacy_receipt_links(connection):
        return {}
    totals = defaultdict(lambda: [0, 0, 0])
    rows = connection.execute('''SELECT f.id,il.net_cents,il.gross_cents,SUM(a.allocated_cents) received
        FROM revenue_fee_items f JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
        JOIN revenue_invoice_lines il ON il.source_kind='fee_item' AND il.source_uid=f.uid AND il.student_id=f.student_id
        JOIN revenue_invoices i ON i.id=il.invoice_id AND i.commercial_case_id=f.commercial_case_id
          AND i.document_kind='client_invoice' AND i.status='issued'
        JOIN revenue_payment_allocations a ON a.invoice_line_id=il.id
        JOIN revenue_payments p ON p.id=a.payment_id AND p.status='confirmed'
          AND p.payment_direction='received' AND p.counterparty_kind='client'
        WHERE c.uid=? AND f.status!='cancelled' AND NOT EXISTS(
            SELECT 1 FROM revenue_client_payment_allocations ca WHERE ca.payment_id=p.id AND ca.fee_item_id=f.id)
        GROUP BY f.id,il.id''', (case_uid,))
    for row in rows:
        base, gross, received = int(row['net_cents']), int(row['gross_cents']), int(row['received'])
        if base == gross:
            totals[row['id']][0] += received
        elif received >= gross:
            totals[row['id']][0] += base
            totals[row['id']][1] += gross - base
        else:
            totals[row['id']][2] += received
    return dict(totals)
