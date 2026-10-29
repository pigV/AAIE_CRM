-- Schema-only canonical ownership for Progression contracts.
-- Historical rows remain NULL/unclassified; no business data is reclassified.

ALTER TABLE progression_cases
ADD COLUMN business_type TEXT
CHECK (
  business_type IS NULL
  OR business_type IN ('higher_education','school_progression')
);

ALTER TABLE progression_cases
ADD COLUMN source_contract_id INTEGER
REFERENCES contracts(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX uq_progression_cases_source_contract
ON progression_cases(source_contract_id)
WHERE source_contract_id IS NOT NULL;

CREATE TRIGGER progression_contract_ownership_insert
BEFORE INSERT ON progression_cases
WHEN
  (NEW.business_type IS NULL AND NEW.source_contract_id IS NOT NULL)
  OR (NEW.business_type='higher_education' AND NEW.source_contract_id IS NOT NULL)
  OR (NEW.business_type='school_progression' AND NEW.source_contract_id IS NULL)
  OR (
    NEW.business_type='school_progression'
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
  OR (NEW.business_type='school_progression' AND NEW.source_contract_id IS NULL)
  OR (
    NEW.business_type='school_progression'
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
