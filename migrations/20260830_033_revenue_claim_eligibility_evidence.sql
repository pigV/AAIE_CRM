-- Revenue & Commission Operational Integration: Claim authority only.
-- Additive, no backfill, and Migration 032 remains immutable.

CREATE TABLE revenue_claim_eligibility_evaluations (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  claim_id INTEGER NOT NULL UNIQUE
    REFERENCES revenue_claims(id) ON DELETE RESTRICT,
  commission_line_id INTEGER NOT NULL
    REFERENCES revenue_commission_lines(id) ON DELETE RESTRICT,
  rule_snapshot_id INTEGER NOT NULL
    REFERENCES revenue_rule_snapshots(id) ON DELETE RESTRICT,
  evaluation_version INTEGER NOT NULL DEFAULT 1 CHECK(evaluation_version>=1),
  result_code TEXT NOT NULL CHECK(result_code IN ('eligible','not_eligible')),
  eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
  evaluator_source TEXT NOT NULL
    CHECK(evaluator_source IN ('system','admin_confirmed')),
  authoritative_facts_json TEXT NOT NULL,
  authoritative_facts_sha256 TEXT NOT NULL CHECK(length(authoritative_facts_sha256)=64),
  reason_code TEXT,
  reason_message TEXT,
  evaluated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  evaluated_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(
    (eligible=1 AND result_code='eligible')
    OR (eligible=0 AND result_code='not_eligible')
  )
);

CREATE TABLE revenue_claim_evidence_links (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  claim_id INTEGER NOT NULL REFERENCES revenue_claims(id) ON DELETE RESTRICT,
  claim_document_requirement_id INTEGER NOT NULL
    REFERENCES revenue_claim_document_requirements(id) ON DELETE RESTRICT,
  revenue_document_id INTEGER NOT NULL
    REFERENCES revenue_documents(id) ON DELETE RESTRICT,
  student_document_id INTEGER REFERENCES student_documents(id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL CHECK(source_kind IN (
    'student_document','revenue_document','coe','commencement','payment','other_authoritative'
  )),
  source_reference TEXT NOT NULL CHECK(length(trim(source_reference))>0),
  verification_status TEXT NOT NULL CHECK(verification_status IN ('verified','rejected')),
  verified_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  verified_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(claim_id,claim_document_requirement_id,revenue_document_id)
);

CREATE TRIGGER revenue_claim_eligibility_evaluations_no_update
BEFORE UPDATE ON revenue_claim_eligibility_evaluations
BEGIN
  SELECT RAISE(ABORT,'revenue claim eligibility evaluations are append-only');
END;

CREATE TRIGGER revenue_claim_eligibility_evaluations_no_delete
BEFORE DELETE ON revenue_claim_eligibility_evaluations
BEGIN
  SELECT RAISE(ABORT,'revenue claim eligibility evaluations are append-only');
END;

CREATE TRIGGER revenue_claim_evidence_links_no_update
BEFORE UPDATE ON revenue_claim_evidence_links
BEGIN
  SELECT RAISE(ABORT,'revenue claim evidence links are append-only');
END;

CREATE TRIGGER revenue_claim_evidence_links_no_delete
BEFORE DELETE ON revenue_claim_evidence_links
BEGIN
  SELECT RAISE(ABORT,'revenue claim evidence links are append-only');
END;
