-- Revenue client Payment -> Fee Item allocation -> immutable Receipt V1.
-- Additive only: no Client Invoice dependency, no historical backfill, no rebuilds.

CREATE TABLE revenue_fee_item_contract_links (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  fee_item_id INTEGER NOT NULL UNIQUE
    REFERENCES revenue_fee_items(id) ON DELETE RESTRICT,
  contract_id INTEGER NOT NULL
    REFERENCES contracts(id) ON DELETE RESTRICT,
  commercial_case_id INTEGER NOT NULL
    REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE TABLE revenue_client_payment_allocations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  payment_id INTEGER NOT NULL UNIQUE
    REFERENCES revenue_payments(id) ON DELETE RESTRICT,
  fee_item_id INTEGER NOT NULL
    REFERENCES revenue_fee_items(id) ON DELETE RESTRICT,
  allocated_base_cents INTEGER NOT NULL CHECK(allocated_base_cents>0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_charge_aud_cents INTEGER NOT NULL
    CHECK(gross_charge_aud_cents=allocated_base_cents+gst_cents),
  payment_method_rule_version_id INTEGER NOT NULL
    REFERENCES revenue_payment_method_rule_versions(id) ON DELETE RESTRICT,
  method_code_snapshot TEXT NOT NULL CHECK(length(trim(method_code_snapshot))>0),
  gst_rate_basis_points INTEGER NOT NULL
    CHECK(gst_rate_basis_points BETWEEN 0 AND 10000),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_revenue_client_payment_allocations_fee_item
ON revenue_client_payment_allocations(fee_item_id,created_at,id);

CREATE TABLE revenue_receipt_snapshots (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  receipt_id INTEGER NOT NULL UNIQUE
    REFERENCES revenue_receipts(id) ON DELETE RESTRICT,
  receipt_number TEXT NOT NULL UNIQUE,
  issuer_name TEXT NOT NULL,
  issuer_abn TEXT NOT NULL,
  student_name TEXT NOT NULL,
  student_number TEXT NOT NULL,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  application_cycle_label TEXT NOT NULL,
  contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE RESTRICT,
  contract_reference TEXT NOT NULL,
  fee_item_id INTEGER NOT NULL REFERENCES revenue_fee_items(id) ON DELETE RESTRICT,
  fee_type TEXT NOT NULL,
  payment_date TEXT NOT NULL,
  payment_method_code TEXT NOT NULL,
  payment_method_label TEXT NOT NULL,
  actual_currency_code TEXT NOT NULL CHECK(length(actual_currency_code)=3),
  actual_amount_cents INTEGER NOT NULL CHECK(actual_amount_cents>0),
  allocated_base_cents INTEGER NOT NULL CHECK(allocated_base_cents>0),
  gst_cents INTEGER NOT NULL CHECK(gst_cents>=0),
  gross_charge_aud_cents INTEGER NOT NULL
    CHECK(gross_charge_aud_cents=allocated_base_cents+gst_cents),
  transaction_reference TEXT,
  issued_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  issued_at TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL UNIQUE CHECK(length(snapshot_sha256)=64),
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_revenue_receipts_payment
ON revenue_receipts(payment_id);

CREATE TRIGGER revenue_fee_item_contract_links_validate
BEFORE INSERT ON revenue_fee_item_contract_links
WHEN NOT EXISTS (
  SELECT 1
    FROM revenue_fee_items f
    JOIN revenue_commercial_cases c ON c.id=NEW.commercial_case_id
    JOIN contracts k ON k.id=NEW.contract_id
   WHERE f.id=NEW.fee_item_id
     AND f.commercial_case_id=c.id
     AND f.student_id=c.student_id
     AND k.student_id=c.student_id
     AND k.application_cycle_id=c.application_cycle_id
     AND k.contract_status='FullyExecuted'
)
BEGIN
  SELECT RAISE(ABORT,'Revenue Fee Item Contract student/cycle mismatch');
END;

CREATE TRIGGER revenue_fee_item_contract_links_no_update
BEFORE UPDATE ON revenue_fee_item_contract_links
BEGIN
  SELECT RAISE(ABORT,'Revenue Fee Item Contract links are append-only');
END;

CREATE TRIGGER revenue_fee_item_contract_links_no_delete
BEFORE DELETE ON revenue_fee_item_contract_links
BEGIN
  SELECT RAISE(ABORT,'Revenue Fee Item Contract links are append-only');
END;

CREATE TRIGGER revenue_client_payment_allocations_validate
BEFORE INSERT ON revenue_client_payment_allocations
WHEN NOT EXISTS (
  SELECT 1
    FROM revenue_payments p
    JOIN revenue_payment_methods m ON m.id=p.payment_method_id
    JOIN revenue_payment_method_rule_versions r
      ON r.id=NEW.payment_method_rule_version_id
     AND r.payment_method_id=m.id
    JOIN revenue_fee_item_contract_links l ON l.fee_item_id=NEW.fee_item_id
   WHERE p.id=NEW.payment_id
     AND p.payment_direction='received'
     AND p.counterparty_kind='client'
     AND p.status='confirmed'
     AND m.method_code=NEW.method_code_snapshot
     AND r.gst_rate_basis_points=NEW.gst_rate_basis_points
)
BEGIN
  SELECT RAISE(ABORT,'Revenue client allocation authority mismatch');
END;

CREATE TRIGGER revenue_client_payment_allocations_no_overallocation
BEFORE INSERT ON revenue_client_payment_allocations
WHEN (
  COALESCE((
    SELECT SUM(a.allocated_base_cents)
      FROM revenue_client_payment_allocations a
     WHERE a.fee_item_id=NEW.fee_item_id
  ),0) + NEW.allocated_base_cents
) > COALESCE((
  SELECT f.net_cents FROM revenue_fee_items f WHERE f.id=NEW.fee_item_id
),-1)
BEGIN
  SELECT RAISE(ABORT,'Revenue client allocation exceeds fee item final base');
END;

CREATE TRIGGER revenue_client_payment_allocations_no_update
BEFORE UPDATE ON revenue_client_payment_allocations
BEGIN
  SELECT RAISE(ABORT,'Revenue client payment allocations are append-only');
END;

CREATE TRIGGER revenue_client_payment_allocations_no_delete
BEFORE DELETE ON revenue_client_payment_allocations
BEGIN
  SELECT RAISE(ABORT,'Revenue client payment allocations are append-only');
END;

CREATE TRIGGER revenue_receipt_snapshots_no_update
BEFORE UPDATE ON revenue_receipt_snapshots
BEGIN
  SELECT RAISE(ABORT,'Revenue Receipt snapshots are append-only');
END;

CREATE TRIGGER revenue_receipt_snapshots_no_delete
BEFORE DELETE ON revenue_receipt_snapshots
BEGIN
  SELECT RAISE(ABORT,'Revenue Receipt snapshots are append-only');
END;

CREATE TRIGGER revenue_receipts_issued_no_update
BEFORE UPDATE ON revenue_receipts
WHEN EXISTS(SELECT 1 FROM revenue_receipt_snapshots s WHERE s.receipt_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT,'Issued Revenue Receipt is immutable');
END;

CREATE TRIGGER revenue_receipts_issued_no_delete
BEFORE DELETE ON revenue_receipts
WHEN EXISTS(SELECT 1 FROM revenue_receipt_snapshots s WHERE s.receipt_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT,'Issued Revenue Receipt is immutable');
END;

CREATE TRIGGER revenue_payments_receipted_no_update
BEFORE UPDATE ON revenue_payments
WHEN EXISTS(SELECT 1 FROM revenue_receipts r WHERE r.payment_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT,'Receipted Revenue Payment is immutable');
END;

CREATE TRIGGER revenue_documents_financial_no_update
BEFORE UPDATE ON revenue_documents
WHEN EXISTS(SELECT 1 FROM revenue_payments p WHERE p.evidence_document_id=OLD.id)
  OR EXISTS(SELECT 1 FROM revenue_receipts r WHERE r.document_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT,'Issued Revenue financial document is immutable');
END;

CREATE TRIGGER revenue_documents_financial_no_delete
BEFORE DELETE ON revenue_documents
WHEN EXISTS(SELECT 1 FROM revenue_payments p WHERE p.evidence_document_id=OLD.id)
  OR EXISTS(SELECT 1 FROM revenue_receipts r WHERE r.document_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT,'Issued Revenue financial document is immutable');
END;
