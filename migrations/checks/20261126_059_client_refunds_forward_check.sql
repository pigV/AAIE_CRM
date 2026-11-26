SELECT
  (SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='revenue_client_cash_movements')=1
  AND (SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN (
    'revenue_client_refund_validate','revenue_client_refund_no_update',
    'revenue_client_refund_no_delete','revenue_client_refund_document_no_update'
  ))=4
  AND NOT EXISTS(
    SELECT 1 FROM revenue_refunds
     WHERE client_payment_allocation_id IS NOT NULL AND (status<>'paid' OR recorded_by IS NULL)
  );
