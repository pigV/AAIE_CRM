SELECT count(*) = 3
FROM sqlite_master
WHERE type='table' AND name IN (
  'revenue_fee_item_contract_links',
  'revenue_client_payment_allocations',
  'revenue_receipt_snapshots'
);

SELECT count(*) = 1
FROM sqlite_master
WHERE type='index' AND name='uq_revenue_receipts_payment';

SELECT count(*) = 14
FROM sqlite_master
WHERE type='trigger' AND name IN (
  'revenue_fee_item_contract_links_validate',
  'revenue_fee_item_contract_links_no_update',
  'revenue_fee_item_contract_links_no_delete',
  'revenue_client_payment_allocations_validate',
  'revenue_client_payment_allocations_no_overallocation',
  'revenue_client_payment_allocations_no_update',
  'revenue_client_payment_allocations_no_delete',
  'revenue_receipt_snapshots_no_update',
  'revenue_receipt_snapshots_no_delete',
  'revenue_receipts_issued_no_update',
  'revenue_receipts_issued_no_delete',
  'revenue_payments_receipted_no_update',
  'revenue_documents_financial_no_update',
  'revenue_documents_financial_no_delete'
);

SELECT count(*) = 0 FROM revenue_fee_item_contract_links;
SELECT count(*) = 0 FROM revenue_client_payment_allocations;
SELECT count(*) = 0 FROM revenue_receipt_snapshots;
SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
