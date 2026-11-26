-- Preserve pre-existing requested/approved refunds. A recorded payout has its own facts.
ALTER TABLE revenue_refunds ADD COLUMN client_payment_allocation_id INTEGER
    REFERENCES revenue_client_payment_allocations(id) ON DELETE RESTRICT;
ALTER TABLE revenue_refunds ADD COLUMN currency_code TEXT CHECK(length(currency_code)=3);
ALTER TABLE revenue_refunds ADD COLUMN refunded_base_cents INTEGER CHECK(refunded_base_cents>=0);
ALTER TABLE revenue_refunds ADD COLUMN refunded_gst_cents INTEGER CHECK(refunded_gst_cents>=0);
ALTER TABLE revenue_refunds ADD COLUMN refunded_at TEXT;
ALTER TABLE revenue_refunds ADD COLUMN recorded_by INTEGER REFERENCES users(id) ON DELETE RESTRICT;

CREATE INDEX idx_revenue_client_paid_refunds
    ON revenue_refunds(payment_uid,client_payment_allocation_id)
    WHERE status='paid';

CREATE TRIGGER revenue_client_refund_validate
BEFORE INSERT ON revenue_refunds
WHEN NEW.client_payment_allocation_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NEW.status<>'paid' OR NEW.refunded_at IS NULL
                    OR NEW.recorded_by IS NULL OR NEW.currency_code IS NULL
                    OR NEW.refunded_base_cents IS NULL OR NEW.refunded_gst_cents IS NULL
                    OR length(trim(NEW.reason))=0
              THEN RAISE(ABORT,'incomplete paid client refund') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM revenue_client_payment_allocations a
      JOIN revenue_payments p ON p.id=a.payment_id
      JOIN revenue_documents d ON d.id=NEW.evidence_document_id
     WHERE a.id=NEW.client_payment_allocation_id AND p.uid=NEW.payment_uid
       AND p.status='confirmed' AND p.payment_direction='received'
       AND p.counterparty_kind='client' AND p.currency_code=NEW.currency_code
       AND d.document_kind='client_refund_evidence'
       AND d.mime_type IN ('application/pdf','image/png','image/jpeg')
       AND julianday(NEW.refunded_at) IS NOT NULL
       AND substr(NEW.refunded_at,1,10)>=p.received_date
       AND (NEW.currency_code<>'AUD' OR NEW.amount_cents=NEW.refunded_base_cents+NEW.refunded_gst_cents)
       AND p.amount_cents>=NEW.amount_cents+COALESCE((
           SELECT SUM(amount_cents) FROM revenue_refunds
            WHERE payment_uid=NEW.payment_uid AND status='paid'),0)
       AND a.allocated_base_cents>=NEW.refunded_base_cents+COALESCE((
           SELECT SUM(refunded_base_cents) FROM revenue_refunds
            WHERE client_payment_allocation_id=a.id AND status='paid'),0)
       AND a.gst_cents>=NEW.refunded_gst_cents+COALESCE((
           SELECT SUM(refunded_gst_cents) FROM revenue_refunds
            WHERE client_payment_allocation_id=a.id AND status='paid'),0)
  ) THEN RAISE(ABORT,'invalid paid client refund allocation') END;
END;

CREATE TRIGGER revenue_client_refund_no_update
BEFORE UPDATE ON revenue_refunds
WHEN OLD.client_payment_allocation_id IS NOT NULL OR NEW.client_payment_allocation_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT,'recorded client refunds are immutable');
END;

CREATE TRIGGER revenue_client_refund_no_delete
BEFORE DELETE ON revenue_refunds WHEN OLD.client_payment_allocation_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT,'recorded client refunds are immutable');
END;

CREATE TRIGGER revenue_client_refund_document_no_update
BEFORE UPDATE ON revenue_documents
WHEN EXISTS(SELECT 1 FROM revenue_refunds r
             WHERE r.evidence_document_id=OLD.id AND r.client_payment_allocation_id IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT,'client refund evidence is immutable');
END;

CREATE VIEW revenue_client_cash_movements AS
  SELECT f.commercial_case_id,a.fee_item_id,p.uid AS payment_uid,
         p.received_date AS effective_date,p.currency_code,'receipt' AS movement_kind,
         p.amount_cents AS actual_cents,a.allocated_base_cents AS base_cents,a.gst_cents AS gst_cents
    FROM revenue_client_payment_allocations a
    JOIN revenue_payments p ON p.id=a.payment_id
    JOIN revenue_fee_items f ON f.id=a.fee_item_id
   WHERE p.status='confirmed' AND p.payment_direction='received' AND p.counterparty_kind='client'
   UNION ALL
  SELECT f.commercial_case_id,a.fee_item_id,r.payment_uid,
         substr(r.refunded_at,1,10),r.currency_code,'refund',
         -r.amount_cents,-r.refunded_base_cents,-r.refunded_gst_cents
    FROM revenue_refunds r
    JOIN revenue_client_payment_allocations a ON a.id=r.client_payment_allocation_id
    JOIN revenue_fee_items f ON f.id=a.fee_item_id
   WHERE r.status='paid';
