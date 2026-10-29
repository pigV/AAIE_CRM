-- migration_mode: sqlite_fk_off_rebuild
-- Schema-only canonical ownership for Existing Transfer contracts.
-- Existing rows are copied byte-for-byte; no contract matching or backfill.

CREATE TABLE application_cycles_rebuild_027 (
  id INTEGER PRIMARY KEY,
  cycle_uid TEXT NOT NULL UNIQUE CHECK(length(trim(cycle_uid)) > 0),
  student_id TEXT REFERENCES students(student_id) ON DELETE RESTRICT,
  parent_form_token TEXT REFERENCES parent_form_tokens(token) ON DELETE RESTRICT,
  source_student_case_id INTEGER REFERENCES student_cases(id) ON DELETE RESTRICT,
  cycle_type TEXT NOT NULL
    CHECK(cycle_type IN ('initial','transfer','progression','future')),
  sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
  status TEXT NOT NULL CHECK(status IN ('active','closed')),
  outcome TEXT CHECK(outcome IS NULL OR outcome IN (
    'granted','refused','completed','withdrawn','cancelled',
    'superseded_by_transfer','superseded_by_progression'
  )),
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_by_name TEXT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_by_name TEXT,
  updated_at TEXT NOT NULL,
  education_stream TEXT NOT NULL DEFAULT 'school_education'
    CHECK(education_stream IN ('school_education','higher_education')),
  CHECK(
    (status='active' AND outcome IS NULL AND closed_at IS NULL)
    OR (status='closed' AND outcome IS NOT NULL AND closed_at IS NOT NULL)
  ),
  CHECK(student_id IS NOT NULL OR parent_form_token IS NOT NULL),
  CHECK(
    (cycle_type='initial' AND source_student_case_id IS NULL)
    OR (cycle_type IN ('transfer','progression') AND source_student_case_id IS NOT NULL)
    OR cycle_type='future'
  )
);

INSERT INTO application_cycles_rebuild_027(
  id,cycle_uid,student_id,parent_form_token,source_student_case_id,cycle_type,
  sequence_no,status,outcome,opened_at,closed_at,created_by_user_id,
  created_by_name,created_at,updated_by_user_id,updated_by_name,updated_at,
  education_stream
)
SELECT
  id,cycle_uid,student_id,parent_form_token,source_student_case_id,cycle_type,
  sequence_no,status,outcome,opened_at,closed_at,created_by_user_id,
  created_by_name,created_at,updated_by_user_id,updated_by_name,updated_at,
  education_stream
FROM application_cycles;

DROP TABLE application_cycles;
ALTER TABLE application_cycles_rebuild_027 RENAME TO application_cycles;

CREATE UNIQUE INDEX uq_application_cycles_active_student_stream
  ON application_cycles(student_id,education_stream)
  WHERE status='active' AND student_id IS NOT NULL;
CREATE UNIQUE INDEX uq_application_cycles_parent_form
  ON application_cycles(parent_form_token)
  WHERE parent_form_token IS NOT NULL;
CREATE UNIQUE INDEX uq_application_cycles_student_case
  ON application_cycles(source_student_case_id)
  WHERE source_student_case_id IS NOT NULL;
CREATE UNIQUE INDEX uq_application_cycles_student_sequence
  ON application_cycles(student_id,sequence_no)
  WHERE student_id IS NOT NULL;
CREATE INDEX idx_application_cycles_status
  ON application_cycles(status,cycle_type,id);
CREATE INDEX idx_application_cycles_student_stream
  ON application_cycles(student_id,education_stream,status,id);

CREATE TRIGGER application_cycles_case_ownership_insert
BEFORE INSERT ON application_cycles
WHEN NEW.source_student_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
  WHERE id=NEW.source_student_case_id
    AND student_id=NEW.student_id AND case_type=NEW.cycle_type
)
BEGIN
  SELECT RAISE(ABORT,'application cycle case ownership mismatch');
END;

CREATE TRIGGER application_cycles_case_ownership_update
BEFORE UPDATE OF source_student_case_id,student_id,cycle_type ON application_cycles
WHEN NEW.source_student_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
  WHERE id=NEW.source_student_case_id
    AND student_id=NEW.student_id AND case_type=NEW.cycle_type
)
BEGIN
  SELECT RAISE(ABORT,'application cycle case ownership mismatch');
END;

CREATE TRIGGER application_cycles_student_attach_once
BEFORE UPDATE OF student_id ON application_cycles
WHEN OLD.student_id IS NOT NULL AND NEW.student_id IS NOT OLD.student_id
BEGIN
  SELECT RAISE(ABORT,'application cycle student identity is immutable');
END;

CREATE TRIGGER application_cycles_no_delete
BEFORE DELETE ON application_cycles
BEGIN
  SELECT RAISE(ABORT,'application cycles are retained permanently');
END;

ALTER TABLE progression_cases
ADD COLUMN current_school_enrolment_id INTEGER
REFERENCES student_school_enrolments(id) ON DELETE RESTRICT;

CREATE INDEX ix_progression_cases_current_enrolment
ON progression_cases(current_school_enrolment_id);

CREATE TRIGGER progression_current_enrolment_ownership_update
BEFORE UPDATE OF case_id,current_school_enrolment_id ON progression_cases
WHEN NEW.current_school_enrolment_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM student_school_enrolments AS enrolment
  JOIN student_cases AS student_case ON student_case.id=NEW.case_id
  WHERE enrolment.id=NEW.current_school_enrolment_id
    AND enrolment.student_id=student_case.student_id
    AND enrolment.enrolment_role='current'
    AND enrolment.status='active'
)
BEGIN
  SELECT RAISE(ABORT,'progression current enrolment ownership mismatch');
END;

CREATE TRIGGER progression_current_enrolment_ownership_insert
BEFORE INSERT ON progression_cases
WHEN NEW.current_school_enrolment_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM student_school_enrolments AS enrolment
  JOIN student_cases AS student_case ON student_case.id=NEW.case_id
  WHERE enrolment.id=NEW.current_school_enrolment_id
    AND enrolment.student_id=student_case.student_id
    AND enrolment.enrolment_role='current'
    AND enrolment.status='active'
)
BEGIN
  SELECT RAISE(ABORT,'progression current enrolment ownership mismatch');
END;

CREATE TABLE transfer_cases_rebuild_027 (
    case_id INTEGER PRIMARY KEY
        REFERENCES student_cases(id) ON DELETE RESTRICT,
    transfer_origin TEXT NOT NULL DEFAULT 'internal'
        CHECK (transfer_origin IN ('internal', 'external')),
    source_contract_id INTEGER
        REFERENCES contracts(id) ON DELETE RESTRICT,
    workflow_state TEXT NOT NULL DEFAULT 'draft'
        CHECK (workflow_state IN (
            'draft','preparing','school_search','target_confirmed',
            'application_submitted','waiting_offer','offer_received',
            'waiting_payment','payment_satisfied',
            'waiting_completion_document','ready_to_complete','paused',
            'completed','cancelled','failed','archived'
        )),
    current_school_name_snapshot TEXT NOT NULL
        CHECK (length(trim(current_school_name_snapshot)) > 0),
    current_school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
    current_city_key TEXT CHECK (
        current_city_key IS NULL OR (
            current_city_key=lower(current_city_key)
            AND current_city_key NOT GLOB '*[^a-z0-9_-]*'
        )
    ),
    current_school_sector TEXT CHECK (
        current_school_sector IS NULL
        OR current_school_sector IN ('public','private','unknown')
    ),
    target_city_key TEXT CHECK (
        target_city_key IS NULL OR (
            target_city_key=lower(target_city_key)
            AND target_city_key NOT GLOB '*[^a-z0-9_-]*'
        )
    ),
    target_school_sector TEXT CHECK (
        target_school_sector IS NULL
        OR target_school_sector IN ('public','private','unknown')
    ),
    rule_status TEXT NOT NULL DEFAULT 'pending_confirmation'
        CHECK (rule_status IN ('pending_confirmation','determined')),
    same_city_result TEXT CHECK (
        same_city_result IS NULL
        OR same_city_result IN ('same_city','cross_city','unknown')
    ),
    sector_transition TEXT CHECK (
        sector_transition IS NULL OR sector_transition IN (
            'public_to_public','public_to_private','private_to_public',
            'private_to_private','unknown'
        )
    ),
    required_completion_document TEXT CHECK (
        required_completion_document IS NULL
        OR required_completion_document IN (
            'confirmation_letter','coe','pending_confirmation'
        )
    ),
    tuition_requirement TEXT CHECK (
        tuition_requirement IS NULL
        OR tuition_requirement IN (
            'not_applicable','deposit_or_first_payment_required',
            'pending_confirmation'
        )
    ),
    final_application_id INTEGER,
    rule_calculated_at TEXT,
    rule_calculated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    rule_version TEXT NOT NULL CHECK (length(trim(rule_version)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT CHECK (
        title IS NULL OR (length(trim(title)) >= 1 AND length(title) <= 200)
    ),
    current_school_enrolment_id INTEGER
        REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
    upcoming_school_enrolment_id INTEGER
        REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
    relationship_status TEXT NOT NULL DEFAULT 'needs_repair'
        CHECK (relationship_status IN (
            'needs_repair','current_ready','ready','transitioned','reverted'
        )),
    CHECK (
        transfer_origin='internal'
        OR (transfer_origin='external' AND source_contract_id IS NOT NULL)
    ),
    CHECK (
        rule_status='pending_confirmation'
        OR (
            rule_status='determined'
            AND same_city_result IS NOT NULL
            AND sector_transition IS NOT NULL
            AND required_completion_document IS NOT NULL
            AND tuition_requirement IS NOT NULL
            AND rule_calculated_at IS NOT NULL
            AND rule_calculated_by IS NOT NULL
        )
    ),
    FOREIGN KEY(final_application_id,case_id)
        REFERENCES transfer_school_applications(id,case_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

INSERT INTO transfer_cases_rebuild_027(
    case_id,transfer_origin,source_contract_id,workflow_state,
    current_school_name_snapshot,current_school_id,current_city_key,
    current_school_sector,target_city_key,target_school_sector,rule_status,
    same_city_result,sector_transition,required_completion_document,
    tuition_requirement,final_application_id,rule_calculated_at,
    rule_calculated_by,rule_version,created_at,updated_at,title,
    current_school_enrolment_id,upcoming_school_enrolment_id,
    relationship_status
)
SELECT
    case_id,transfer_origin,source_contract_id,workflow_state,
    current_school_name_snapshot,current_school_id,current_city_key,
    current_school_sector,target_city_key,target_school_sector,rule_status,
    same_city_result,sector_transition,required_completion_document,
    tuition_requirement,final_application_id,rule_calculated_at,
    rule_calculated_by,rule_version,created_at,updated_at,title,
    current_school_enrolment_id,upcoming_school_enrolment_id,
    relationship_status
FROM transfer_cases;

DROP TABLE transfer_cases;
ALTER TABLE transfer_cases_rebuild_027 RENAME TO transfer_cases;

CREATE UNIQUE INDEX uq_transfer_cases_external_contract
    ON transfer_cases(source_contract_id)
    WHERE source_contract_id IS NOT NULL;
CREATE INDEX ix_transfer_cases_workflow
    ON transfer_cases(workflow_state,updated_at,case_id);
CREATE INDEX ix_transfer_cases_current_enrolment
    ON transfer_cases(current_school_enrolment_id);
CREATE INDEX ix_transfer_cases_upcoming_enrolment
    ON transfer_cases(upcoming_school_enrolment_id);
CREATE INDEX ix_transfer_cases_relationship_status
    ON transfer_cases(relationship_status,case_id);

CREATE TRIGGER transfer_cases_require_transfer_parent_insert
BEFORE INSERT ON transfer_cases
WHEN NOT EXISTS (
    SELECT 1 FROM student_cases
    WHERE id=NEW.case_id AND case_type='transfer'
)
BEGIN
    SELECT RAISE(ABORT,'transfer case requires transfer student case');
END;

CREATE TRIGGER transfer_cases_require_transfer_parent_update
BEFORE UPDATE OF case_id ON transfer_cases
BEGIN
    SELECT RAISE(ABORT,'transfer case identity is immutable');
END;

CREATE TRIGGER transfer_cases_contract_ownership_insert
BEFORE INSERT ON transfer_cases
WHEN
    (NEW.transfer_origin='external' AND NEW.source_contract_id IS NULL)
    OR (
        NEW.source_contract_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM contracts AS contract
            JOIN student_cases AS student_case ON student_case.id=NEW.case_id
            WHERE contract.id=NEW.source_contract_id
              AND contract.student_id=student_case.student_id
              AND contract.contract_type='TransferApplicationContract'
              AND contract.contract_status='FullyExecuted'
              AND contract.deleted_at IS NULL
              AND contract.voided_at IS NULL
        )
    )
BEGIN
    SELECT RAISE(ABORT,'transfer contract ownership mismatch');
END;

CREATE TRIGGER transfer_cases_contract_ownership_update
BEFORE UPDATE OF case_id,transfer_origin,source_contract_id ON transfer_cases
WHEN
    (NEW.transfer_origin='external' AND NEW.source_contract_id IS NULL)
    OR (OLD.source_contract_id IS NOT NULL AND NEW.source_contract_id IS NOT OLD.source_contract_id)
    OR (
        NEW.source_contract_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM contracts AS contract
            JOIN student_cases AS student_case ON student_case.id=NEW.case_id
            WHERE contract.id=NEW.source_contract_id
              AND contract.student_id=student_case.student_id
              AND contract.contract_type='TransferApplicationContract'
              AND contract.contract_status='FullyExecuted'
              AND contract.deleted_at IS NULL
              AND contract.voided_at IS NULL
        )
    )
BEGIN
    SELECT RAISE(ABORT,'transfer contract ownership mismatch');
END;

CREATE TRIGGER transfer_cases_final_application_insert_guard
BEFORE INSERT ON transfer_cases
WHEN NEW.final_application_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM transfer_school_applications
    WHERE id=NEW.final_application_id AND case_id=NEW.case_id AND is_final=1
)
BEGIN
    SELECT RAISE(ABORT,'final application must be final and belong to case');
END;

CREATE TRIGGER transfer_cases_final_application_update_guard
BEFORE UPDATE OF final_application_id ON transfer_cases
WHEN (
    NEW.final_application_id IS NULL AND EXISTS (
        SELECT 1 FROM transfer_school_applications
        WHERE case_id=NEW.case_id AND is_final=1
    )
) OR (
    NEW.final_application_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM transfer_school_applications
        WHERE id=NEW.final_application_id AND case_id=NEW.case_id AND is_final=1
    )
)
BEGIN
    SELECT RAISE(ABORT,'final application must be final and belong to case');
END;

CREATE TRIGGER transfer_cases_relationship_validate_insert
BEFORE INSERT ON transfer_cases
BEGIN
    SELECT CASE WHEN NOT (
        (NEW.relationship_status='needs_repair'
            AND NEW.current_school_enrolment_id IS NULL
            AND NEW.upcoming_school_enrolment_id IS NULL)
        OR (NEW.relationship_status IN ('current_ready','transitioned','reverted')
            AND NEW.current_school_enrolment_id IS NOT NULL
            AND NEW.upcoming_school_enrolment_id IS NULL)
        OR (NEW.relationship_status='ready'
            AND NEW.current_school_enrolment_id IS NOT NULL
            AND NEW.upcoming_school_enrolment_id IS NOT NULL)
    ) THEN RAISE(ABORT,'transfer relationship status and pointers conflict') END;
    SELECT CASE WHEN NEW.current_school_enrolment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_school_enrolments AS enrolment
        JOIN student_cases AS parent_case ON parent_case.id=NEW.case_id
        WHERE enrolment.id=NEW.current_school_enrolment_id
          AND enrolment.student_id=parent_case.student_id
          AND enrolment.enrolment_role='current' AND enrolment.status='active'
    ) THEN RAISE(ABORT,'current enrolment must be active current for the case student') END;
    SELECT CASE WHEN NEW.upcoming_school_enrolment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_school_enrolments AS enrolment
        JOIN student_cases AS parent_case ON parent_case.id=NEW.case_id
        WHERE enrolment.id=NEW.upcoming_school_enrolment_id
          AND enrolment.student_id=parent_case.student_id
          AND enrolment.enrolment_role='upcoming' AND enrolment.status='active'
          AND enrolment.source_case_id=NEW.case_id
    ) THEN RAISE(ABORT,'upcoming enrolment must be active upcoming for the same case student') END;
    SELECT CASE WHEN NEW.current_school_enrolment_id IS NEW.upcoming_school_enrolment_id
        AND NEW.current_school_enrolment_id IS NOT NULL
        THEN RAISE(ABORT,'current and upcoming enrolments must differ') END;
END;

CREATE TRIGGER transfer_cases_relationship_validate_update
BEFORE UPDATE OF current_school_enrolment_id,upcoming_school_enrolment_id,relationship_status
ON transfer_cases
BEGIN
    SELECT CASE WHEN NOT (
        (NEW.relationship_status='needs_repair'
            AND NEW.current_school_enrolment_id IS NULL
            AND NEW.upcoming_school_enrolment_id IS NULL)
        OR (NEW.relationship_status IN ('current_ready','transitioned','reverted')
            AND NEW.current_school_enrolment_id IS NOT NULL
            AND NEW.upcoming_school_enrolment_id IS NULL)
        OR (NEW.relationship_status='ready'
            AND NEW.current_school_enrolment_id IS NOT NULL
            AND NEW.upcoming_school_enrolment_id IS NOT NULL)
    ) THEN RAISE(ABORT,'transfer relationship status and pointers conflict') END;
    SELECT CASE WHEN NEW.current_school_enrolment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_school_enrolments AS enrolment
        JOIN student_cases AS parent_case ON parent_case.id=NEW.case_id
        WHERE enrolment.id=NEW.current_school_enrolment_id
          AND enrolment.student_id=parent_case.student_id
          AND enrolment.enrolment_role='current' AND enrolment.status='active'
    ) THEN RAISE(ABORT,'current enrolment must be active current for the case student') END;
    SELECT CASE WHEN NEW.upcoming_school_enrolment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM student_school_enrolments AS enrolment
        JOIN student_cases AS parent_case ON parent_case.id=NEW.case_id
        WHERE enrolment.id=NEW.upcoming_school_enrolment_id
          AND enrolment.student_id=parent_case.student_id
          AND enrolment.enrolment_role='upcoming' AND enrolment.status='active'
          AND enrolment.source_case_id=NEW.case_id
    ) THEN RAISE(ABORT,'upcoming enrolment must be active upcoming for the same case student') END;
    SELECT CASE WHEN NEW.current_school_enrolment_id IS NEW.upcoming_school_enrolment_id
        AND NEW.current_school_enrolment_id IS NOT NULL
        THEN RAISE(ABORT,'current and upcoming enrolments must differ') END;
END;

CREATE TRIGGER transfer_cases_no_delete
BEFORE DELETE ON transfer_cases
BEGIN
    SELECT RAISE(ABORT,'transfer cases are retained permanently');
END;

DROP TRIGGER IF EXISTS student_school_enrolments_transfer_reference_guard;

CREATE TRIGGER student_school_enrolments_transfer_reference_guard
BEFORE UPDATE OF student_id,enrolment_role,status,source_case_id
ON student_school_enrolments
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM transfer_cases AS transfer
        JOIN student_cases AS parent_case ON parent_case.id=transfer.case_id
        WHERE transfer.current_school_enrolment_id=OLD.id
          AND NOT (
              parent_case.active_slot=0
              AND parent_case.status IN ('completed','cancelled','failed','archived')
          )
          AND (
              NEW.student_id<>parent_case.student_id
              OR NEW.enrolment_role<>'current'
              OR NEW.status<>'active'
          )
    ) THEN RAISE(
        ABORT,'referenced current enrolment cannot invalidate transfer relationship'
    ) END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM transfer_cases AS transfer
        JOIN student_cases AS parent_case ON parent_case.id=transfer.case_id
        WHERE transfer.upcoming_school_enrolment_id=OLD.id
          AND NOT (
              parent_case.active_slot=0
              AND parent_case.status IN ('completed','cancelled','failed','archived')
          )
          AND (
              NEW.student_id<>parent_case.student_id
              OR NEW.enrolment_role<>'upcoming'
              OR NEW.status<>'active'
              OR NEW.source_case_id IS NOT transfer.case_id
          )
    ) THEN RAISE(
        ABORT,'referenced upcoming enrolment cannot invalidate transfer relationship'
    ) END;
END;
