-- Valid only before Migration 034 has created any business authority rows.
-- The guard fails closed instead of deleting financial history.

CREATE TEMP TABLE revenue_034_rollback_guard(value INTEGER);
CREATE TEMP TRIGGER revenue_034_rollback_guard_nonempty
BEFORE INSERT ON revenue_034_rollback_guard
WHEN EXISTS(SELECT 1 FROM revenue_fee_item_contract_links)
  OR EXISTS(SELECT 1 FROM revenue_client_payment_allocations)
  OR EXISTS(SELECT 1 FROM revenue_receipt_snapshots)
BEGIN
  SELECT RAISE(ABORT,'Migration 034 rollback blocked: Revenue client payment authority exists');
END;
INSERT INTO revenue_034_rollback_guard(value) VALUES(1);
DROP TRIGGER revenue_034_rollback_guard_nonempty;
DROP TABLE revenue_034_rollback_guard;

DROP TRIGGER IF EXISTS revenue_receipt_snapshots_no_delete;
DROP TRIGGER IF EXISTS revenue_receipt_snapshots_no_update;
DROP TRIGGER IF EXISTS revenue_receipts_issued_no_delete;
DROP TRIGGER IF EXISTS revenue_receipts_issued_no_update;
DROP TRIGGER IF EXISTS revenue_payments_receipted_no_update;
DROP TRIGGER IF EXISTS revenue_documents_financial_no_delete;
DROP TRIGGER IF EXISTS revenue_documents_financial_no_update;
DROP TRIGGER IF EXISTS revenue_client_payment_allocations_no_delete;
DROP TRIGGER IF EXISTS revenue_client_payment_allocations_no_update;
DROP TRIGGER IF EXISTS revenue_client_payment_allocations_no_overallocation;
DROP TRIGGER IF EXISTS revenue_client_payment_allocations_validate;
DROP TRIGGER IF EXISTS revenue_fee_item_contract_links_no_delete;
DROP TRIGGER IF EXISTS revenue_fee_item_contract_links_no_update;
DROP TRIGGER IF EXISTS revenue_fee_item_contract_links_validate;

DROP INDEX IF EXISTS uq_revenue_receipts_payment;
DROP INDEX IF EXISTS idx_revenue_client_payment_allocations_fee_item;
DROP TABLE IF EXISTS revenue_receipt_snapshots;
DROP TABLE IF EXISTS revenue_client_payment_allocations;
DROP TABLE IF EXISTS revenue_fee_item_contract_links;
