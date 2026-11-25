-- Add Visa receipts without rebuilding or rewriting existing contract receipt snapshots.
CREATE TABLE revenue_visa_receipt_snapshots (
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
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE RESTRICT,
  visa_case_reference TEXT NOT NULL CHECK(length(trim(visa_case_reference))>0),
  visa_subclass TEXT NOT NULL CHECK(length(trim(visa_subclass))>0),
  applicant_name TEXT NOT NULL CHECK(length(trim(applicant_name))>0),
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

CREATE TRIGGER revenue_visa_receipt_snapshots_validate BEFORE INSERT ON revenue_visa_receipt_snapshots
WHEN NOT EXISTS (
 SELECT 1 FROM revenue_receipts r JOIN revenue_payments p ON p.id=r.payment_id
 JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
 JOIN revenue_fee_items f ON f.id=a.fee_item_id
 JOIN revenue_rule_snapshots rules ON rules.id=f.rule_snapshot_id
 JOIN json_each(rules.canonical_json,'$.items') rule
 JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id AND c.student_id=f.student_id
 JOIN revenue_fee_item_visa_links l ON l.fee_item_id=f.id AND l.commercial_case_id=c.id
 JOIN visa_cases v ON v.id=l.visa_case_id AND v.student_id=c.student_id AND v.application_cycle_id=c.application_cycle_id
 WHERE r.id=NEW.receipt_id AND r.receipt_number=NEW.receipt_number
 AND r.amount_cents=p.amount_cents AND r.currency_code=p.currency_code
 AND p.payment_direction='received' AND p.counterparty_kind='client' AND p.status='confirmed'
 AND p.billing_account_id=f.billing_account_id
 AND ((a.method_code_snapshot IN ('alipay','wechat') AND p.currency_code='CNY')
      OR (a.method_code_snapshot IN ('au_personal_account','au_company_account')
          AND p.currency_code='AUD' AND p.amount_cents=a.gross_charge_aud_cents))
 AND f.status<>'cancelled' AND f.currency_code='AUD'
 AND json_array_length(rules.canonical_json,'$.items')=1
 AND json_extract(rule.value,'$.visa_case_id')=v.id
 AND json_extract(rule.value,'$.application_cycle_id')=c.application_cycle_id
 AND json_extract(rule.value,'$.milestone_code')=f.milestone_code
 AND json_extract(rule.value,'$.net_cents')=f.net_cents
 AND coalesce(json_extract(rule.value,'$.gst_cents'),0)=f.gst_cents
 AND json_extract(rule.value,'$.contract_id') IS NULL
 AND f.milestone_code IN ('standalone-student-visa','student-visa-renewal','guardian-590','guardian-590-renewal','visitor-600')
 AND NEW.visa_case_reference='VISA-' || v.id
 AND NEW.fee_item_id=f.id AND NEW.visa_case_id=v.id AND NEW.application_cycle_id=c.application_cycle_id
 AND NEW.student_number=c.student_id AND NEW.applicant_name=v.applicant_name
 AND v.archived_at IS NULL
 AND NEW.actual_amount_cents=p.amount_cents AND NEW.actual_currency_code=p.currency_code
 AND NEW.allocated_base_cents=a.allocated_base_cents AND NEW.gst_cents=a.gst_cents
 AND NEW.gross_charge_aud_cents=a.gross_charge_aud_cents AND NEW.payment_date=p.received_date
 AND NEW.payment_method_code=a.method_code_snapshot
 AND NOT EXISTS(SELECT 1 FROM revenue_receipt_snapshots old WHERE old.receipt_id=r.id)
)
BEGIN SELECT RAISE(ABORT,'Visa Receipt snapshot authority mismatch'); END;
CREATE TRIGGER revenue_visa_receipt_snapshots_no_update BEFORE UPDATE ON revenue_visa_receipt_snapshots
BEGIN SELECT RAISE(ABORT,'Visa Receipt snapshots are append-only'); END;
CREATE TRIGGER revenue_visa_receipt_snapshots_no_delete BEFORE DELETE ON revenue_visa_receipt_snapshots
BEGIN SELECT RAISE(ABORT,'Visa Receipt snapshots are append-only'); END;
CREATE TRIGGER revenue_contract_receipt_exclusive BEFORE INSERT ON revenue_receipt_snapshots
WHEN EXISTS(SELECT 1 FROM revenue_visa_receipt_snapshots s WHERE s.receipt_id=NEW.receipt_id)
BEGIN SELECT RAISE(ABORT,'Receipt already has a Visa snapshot'); END;
DROP TRIGGER revenue_receipts_issued_no_update;
DROP TRIGGER revenue_receipts_issued_no_delete;
CREATE TRIGGER revenue_receipts_issued_no_update BEFORE UPDATE ON revenue_receipts
WHEN EXISTS(SELECT 1 FROM revenue_receipt_snapshots s WHERE s.receipt_id=OLD.id)
 OR EXISTS(SELECT 1 FROM revenue_visa_receipt_snapshots s WHERE s.receipt_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'Issued Revenue Receipt is immutable'); END;
CREATE TRIGGER revenue_receipts_issued_no_delete BEFORE DELETE ON revenue_receipts
WHEN EXISTS(SELECT 1 FROM revenue_receipt_snapshots s WHERE s.receipt_id=OLD.id)
 OR EXISTS(SELECT 1 FROM revenue_visa_receipt_snapshots s WHERE s.receipt_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'Issued Revenue Receipt is immutable'); END;
