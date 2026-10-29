CREATE TABLE lifecycle_module_disposition_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE
    CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  module_key TEXT NOT NULL CHECK(module_key IN (
    'applications','offer','coe_oshc','visa','payments','homestay_airport'
  )),
  disposition TEXT NOT NULL CHECK(disposition IN (
    'required','existing','not_applicable'
  )),
  reason TEXT,
  source_record_type TEXT CHECK(source_record_type IS NULL OR source_record_type IN (
    'transfer_school_application','progression_school_application',
    'higher_education_application','offer','coe','oshc','visa','payment',
    'student_document'
  )),
  source_record_id INTEGER,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at)) > 0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
  CHECK(
    (disposition='required'
      AND source_record_type IS NULL AND source_record_id IS NULL)
    OR
    (disposition='existing'
      AND source_record_type IS NOT NULL AND source_record_id IS NOT NULL
      AND reason IS NOT NULL AND length(trim(reason)) > 0)
    OR
    (disposition='not_applicable'
      AND source_record_type IS NULL AND source_record_id IS NULL
      AND reason IS NOT NULL AND length(trim(reason)) > 0)
  )
);

CREATE INDEX idx_lifecycle_module_disposition_current
ON lifecycle_module_disposition_events(
  application_cycle_id,module_key,occurred_at DESC,id DESC
);

CREATE INDEX idx_lifecycle_module_disposition_source
ON lifecycle_module_disposition_events(source_record_type,source_record_id,id)
WHERE source_record_type IS NOT NULL;

CREATE TRIGGER lifecycle_module_disposition_events_no_update
BEFORE UPDATE ON lifecycle_module_disposition_events
BEGIN
  SELECT RAISE(ABORT, 'lifecycle module disposition events are append-only');
END;

CREATE TRIGGER lifecycle_module_disposition_events_no_delete
BEFORE DELETE ON lifecycle_module_disposition_events
BEGIN
  SELECT RAISE(ABORT, 'lifecycle module disposition events are append-only');
END;

-- A Lifecycle now exists as soon as staff create a Progression record.  A
-- school-progression contract remains authoritative when it is linked, but a
-- missing/not-yet-ready contract is a downstream blocker rather than a reason
-- to roll back Case + Lifecycle creation.
DROP TRIGGER IF EXISTS progression_contract_ownership_insert;
DROP TRIGGER IF EXISTS progression_contract_ownership_update;

CREATE TRIGGER progression_contract_ownership_insert
BEFORE INSERT ON progression_cases
WHEN
  (NEW.business_type IS NULL AND NEW.source_contract_id IS NOT NULL)
  OR (NEW.business_type='higher_education' AND NEW.source_contract_id IS NOT NULL)
  OR (
    NEW.business_type='school_progression'
    AND NEW.source_contract_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM contracts AS contract
      JOIN student_cases AS student_case
        ON student_case.id=NEW.case_id
      WHERE contract.id=NEW.source_contract_id
        AND contract.student_id=student_case.student_id
        AND contract.contract_type='LocalStudentApplicationContract'
        AND contract.contract_status='FullyExecuted'
        AND contract.deleted_at IS NULL
        AND contract.voided_at IS NULL
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'progression contract ownership mismatch');
END;

CREATE TRIGGER progression_contract_ownership_update
BEFORE UPDATE OF case_id,business_type,source_contract_id ON progression_cases
WHEN
  (NEW.business_type IS NULL AND NEW.source_contract_id IS NOT NULL)
  OR (NEW.business_type='higher_education' AND NEW.source_contract_id IS NOT NULL)
  OR (
    NEW.business_type='school_progression'
    AND NEW.source_contract_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM contracts AS contract
      JOIN student_cases AS student_case
        ON student_case.id=NEW.case_id
      WHERE contract.id=NEW.source_contract_id
        AND contract.student_id=student_case.student_id
        AND contract.contract_type='LocalStudentApplicationContract'
        AND contract.contract_status='FullyExecuted'
        AND contract.deleted_at IS NULL
        AND contract.voided_at IS NULL
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'progression contract ownership mismatch');
END;
