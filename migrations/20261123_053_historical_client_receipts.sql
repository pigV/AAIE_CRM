CREATE TABLE revenue_historical_receipts (
 id INTEGER PRIMARY KEY,
 uid TEXT NOT NULL UNIQUE,
 student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
 application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
 service_kind TEXT NOT NULL CHECK(service_kind IN ('client_service','student_visa')),
 visa_case_id INTEGER REFERENCES visa_cases(id) ON DELETE RESTRICT,
 status TEXT NOT NULL CHECK(status IN ('draft','confirmed','cancelled')),
 revision INTEGER NOT NULL CHECK(revision>0),
 claimed_received INTEGER NOT NULL CHECK(claimed_received IN (0,1)),
 agreed_aud_cents INTEGER CHECK(agreed_aud_cents IS NULL OR agreed_aud_cents>=0),
 actual_cents INTEGER CHECK(actual_cents IS NULL OR actual_cents>0),
 currency_code TEXT CHECK(currency_code IS NULL OR currency_code IN ('AUD','CNY')),
 settled_aud_cents INTEGER CHECK(settled_aud_cents IS NULL OR settled_aud_cents>=0),
 received_date TEXT,
 method_code TEXT,
 contract_state TEXT NOT NULL CHECK(contract_state IN ('documented','historical_missing','not_required')),
 source_note TEXT NOT NULL,
 payment_id INTEGER UNIQUE REFERENCES revenue_payments(id) ON DELETE RESTRICT,
 commercial_case_id INTEGER REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
 created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 updated_at TEXT NOT NULL,
 UNIQUE(application_cycle_id,service_kind)
);
CREATE TABLE revenue_historical_receipt_evidence (
 id INTEGER PRIMARY KEY,
 uid TEXT NOT NULL UNIQUE,
 historical_receipt_id INTEGER NOT NULL REFERENCES revenue_historical_receipts(id) ON DELETE RESTRICT,
 document_id INTEGER NOT NULL REFERENCES revenue_documents(id) ON DELETE RESTRICT,
 evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('payment_evidence','external_receipt')),
 created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL,
 UNIQUE(historical_receipt_id,document_id)
);
CREATE TRIGGER historical_receipt_confirmed_no_update BEFORE UPDATE ON revenue_historical_receipts
 WHEN OLD.status='confirmed' BEGIN SELECT RAISE(ABORT,'已确认历史收款不可覆盖'); END;
CREATE TRIGGER historical_receipt_no_delete BEFORE DELETE ON revenue_historical_receipts
 BEGIN SELECT RAISE(ABORT,'历史核对记录不可删除'); END;
CREATE TRIGGER historical_receipt_evidence_no_update BEFORE UPDATE ON revenue_historical_receipt_evidence
 BEGIN SELECT RAISE(ABORT,'历史凭证不可覆盖'); END;
CREATE TRIGGER historical_receipt_evidence_no_delete BEFORE DELETE ON revenue_historical_receipt_evidence
 BEGIN SELECT RAISE(ABORT,'历史凭证不可删除'); END;
