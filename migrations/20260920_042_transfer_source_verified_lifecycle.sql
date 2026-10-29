-- Append-only verified lifecycle for Transfer source authority.
-- Existing pending rows remain pending; existing consumed rows remain byte-for-field equivalent.
DROP TRIGGER IF EXISTS transfer_source_authority_pending_insert_036;
DROP TRIGGER IF EXISTS transfer_source_authority_pre_case_insert_036;
DROP TRIGGER IF EXISTS transfer_source_authority_identity_immutable_036;
DROP TRIGGER IF EXISTS transfer_source_authority_no_delete_036;
DROP TRIGGER IF EXISTS transfer_source_authority_validate_insert_036;
DROP TRIGGER IF EXISTS transfer_source_authority_validate_update_036;
DROP TRIGGER IF EXISTS transfer_source_authority_consume_036;
DROP TRIGGER IF EXISTS transfer_source_parent_intake_guard_036;
DROP TRIGGER IF EXISTS transfer_source_case_projection_guard_036;
DROP TRIGGER IF EXISTS transfer_source_origin_projection_guard_036;
DROP TRIGGER IF EXISTS transfer_source_enrolment_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_ordinary_cycle_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_ordinary_contract_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_private_application_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_public_application_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_transfer_cycle_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_transfer_contract_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_authority_no_replace_036;
DROP TRIGGER IF EXISTS transfer_source_origin_no_replace_036;
DROP TRIGGER IF EXISTS transfer_source_student_case_identity_guard_036;
DROP TRIGGER IF EXISTS transfer_source_case_insert_projection_guard_036;
ALTER TABLE transfer_source_authorities RENAME TO transfer_source_authorities_040;
CREATE TABLE transfer_source_authorities (
  id INTEGER PRIMARY KEY,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  transfer_contract_id INTEGER NOT NULL UNIQUE REFERENCES contracts(id) ON DELETE RESTRICT,
  parent_form_token TEXT REFERENCES parent_form_tokens(token) ON DELETE RESTRICT,
  current_enrolment_id INTEGER REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
  ordinary_relationship_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT,
  ordinary_relationship_contract_id INTEGER REFERENCES contracts(id) ON DELETE RESTRICT,
  private_school_application_id INTEGER REFERENCES private_school_applications(id) ON DELETE RESTRICT,
  public_application_group_id INTEGER REFERENCES public_application_groups(id) ON DELETE RESTRICT,
  source_classification TEXT CHECK(source_classification IS NULL OR source_classification IN ('new_transfer','aaie_existing')),
  lifecycle_state TEXT NOT NULL DEFAULT 'pending' CHECK(lifecycle_state IN ('pending','verified','consumed')),
  application_cycle_id INTEGER UNIQUE REFERENCES application_cycles(id) ON DELETE RESTRICT,
  rule_version TEXT NOT NULL CHECK(length(trim(rule_version))>0),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  verified_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  verified_at TEXT CHECK(verified_at IS NULL OR length(trim(verified_at))>0),
  CHECK (
    (parent_form_token IS NOT NULL AND current_enrolment_id IS NULL
     AND ordinary_relationship_cycle_id IS NULL AND ordinary_relationship_contract_id IS NULL
     AND private_school_application_id IS NULL AND public_application_group_id IS NULL)
    OR
    (parent_form_token IS NULL AND current_enrolment_id IS NOT NULL
     AND ordinary_relationship_cycle_id IS NOT NULL
     AND ((ordinary_relationship_contract_id IS NOT NULL)+(private_school_application_id IS NOT NULL)+(public_application_group_id IS NOT NULL))=1)
  ),
  CHECK (
    (lifecycle_state='pending' AND source_classification IS NULL AND application_cycle_id IS NULL
     AND verified_by IS NULL AND verified_at IS NULL)
    OR
    (lifecycle_state='verified' AND source_classification IS NOT NULL AND application_cycle_id IS NULL
     AND verified_by IS NOT NULL AND verified_at IS NOT NULL
     AND ((source_classification='new_transfer' AND parent_form_token IS NOT NULL)
       OR (source_classification='aaie_existing' AND parent_form_token IS NULL)))
    OR
    (lifecycle_state='consumed' AND source_classification IS NOT NULL AND application_cycle_id IS NOT NULL
     AND verified_by IS NOT NULL AND verified_at IS NOT NULL
     AND ((source_classification='new_transfer' AND parent_form_token IS NOT NULL)
       OR (source_classification='aaie_existing' AND parent_form_token IS NULL)))
  )
);
INSERT INTO transfer_source_authorities(id,student_id,transfer_contract_id,parent_form_token,current_enrolment_id,
ordinary_relationship_cycle_id,ordinary_relationship_contract_id,private_school_application_id,
public_application_group_id,source_classification,lifecycle_state,application_cycle_id,rule_version,
created_by,created_at,verified_by,verified_at) SELECT id,student_id,transfer_contract_id,parent_form_token,current_enrolment_id,
ordinary_relationship_cycle_id,ordinary_relationship_contract_id,private_school_application_id,
public_application_group_id,source_classification,lifecycle_state,application_cycle_id,rule_version,
created_by,created_at,verified_by,verified_at FROM transfer_source_authorities_040;
DROP TABLE transfer_source_authorities_040;
CREATE INDEX idx_transfer_source_authority_student ON transfer_source_authorities(student_id,id);
CREATE INDEX idx_transfer_source_authority_intake ON transfer_source_authorities(parent_form_token) WHERE parent_form_token IS NOT NULL;

CREATE TRIGGER transfer_source_authority_pending_insert_041
BEFORE INSERT ON transfer_source_authorities
WHEN NEW.lifecycle_state<>'pending'
BEGIN SELECT RAISE(ABORT,'Transfer source authority must start pending'); END;

CREATE TRIGGER transfer_source_authority_pre_case_insert_041
BEFORE INSERT ON transfer_source_authorities
WHEN EXISTS(SELECT 1 FROM contracts WHERE id=NEW.transfer_contract_id AND application_cycle_id IS NOT NULL)
 OR EXISTS(SELECT 1 FROM transfer_cases WHERE source_contract_id=NEW.transfer_contract_id)
BEGIN SELECT RAISE(ABORT,'Transfer source authority requires an unbound pre-Case contract'); END;

CREATE TRIGGER transfer_source_authority_verify_041
BEFORE UPDATE ON transfer_source_authorities
WHEN OLD.lifecycle_state='pending' AND NEW.lifecycle_state='verified'
BEGIN
  SELECT CASE WHEN NEW.application_cycle_id IS NOT NULL
    OR NEW.verified_by IS NULL OR length(trim(COALESCE(NEW.verified_at,'')))=0
  THEN RAISE(ABORT,'Verified Transfer source requires audit identity without a Cycle') END;
  SELECT CASE WHEN NEW.parent_form_token IS NOT NULL AND NEW.source_classification<>'new_transfer'
    THEN RAISE(ABORT,'Verified Parent intake must classify New Transfer') END;
  SELECT CASE WHEN NEW.parent_form_token IS NULL AND NEW.source_classification<>'aaie_existing'
    THEN RAISE(ABORT,'Verified dual evidence must classify AAIE Existing') END;
END;

CREATE TRIGGER transfer_source_authority_identity_immutable_041
BEFORE UPDATE ON transfer_source_authorities
WHEN OLD.lifecycle_state='consumed'
 OR NOT ((OLD.lifecycle_state='pending' AND NEW.lifecycle_state='verified')
      OR (OLD.lifecycle_state='verified' AND NEW.lifecycle_state='consumed'))
 OR NEW.id IS NOT OLD.id OR NEW.student_id IS NOT OLD.student_id
 OR NEW.transfer_contract_id IS NOT OLD.transfer_contract_id
 OR NEW.parent_form_token IS NOT OLD.parent_form_token
 OR NEW.current_enrolment_id IS NOT OLD.current_enrolment_id
 OR NEW.ordinary_relationship_cycle_id IS NOT OLD.ordinary_relationship_cycle_id
 OR NEW.ordinary_relationship_contract_id IS NOT OLD.ordinary_relationship_contract_id
 OR NEW.private_school_application_id IS NOT OLD.private_school_application_id
 OR NEW.public_application_group_id IS NOT OLD.public_application_group_id
 OR NEW.rule_version IS NOT OLD.rule_version OR NEW.created_by IS NOT OLD.created_by
 OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.lifecycle_state='verified' AND (
      NEW.source_classification IS NOT OLD.source_classification
      OR NEW.verified_by IS NOT OLD.verified_by OR NEW.verified_at IS NOT OLD.verified_at))
BEGIN SELECT RAISE(ABORT,'Transfer source evidence is immutable'); END;

CREATE TRIGGER transfer_source_authority_no_delete_041
BEFORE DELETE ON transfer_source_authorities
BEGIN SELECT RAISE(ABORT,'Transfer source evidence is retained'); END;

CREATE TRIGGER transfer_source_authority_validate_insert_041
BEFORE INSERT ON transfer_source_authorities
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM contracts WHERE id=NEW.transfer_contract_id
      AND student_id=NEW.student_id AND contract_type='TransferApplicationContract'
      AND deleted_at IS NULL AND voided_at IS NULL
  ) THEN RAISE(ABORT,'Transfer source contract ownership mismatch') END;
  SELECT CASE WHEN NEW.parent_form_token IS NOT NULL AND (
    NOT EXISTS (SELECT 1 FROM parent_form_tokens WHERE token=NEW.parent_form_token
      AND student_id=NEW.student_id AND intake_type='new_student_transfer'
      AND invalidated_at IS NULL AND length(trim(COALESCE(submitted_at,'')))>0)
    OR EXISTS (SELECT 1 FROM transfer_existing_source_evidence WHERE student_id=NEW.student_id)
  ) THEN RAISE(ABORT,'Transfer new intake authority missing or conflicting') END;
  SELECT CASE WHEN NEW.parent_form_token IS NULL AND (
    (SELECT count(*) FROM student_school_enrolments WHERE student_id=NEW.student_id
      AND enrolment_role='current' AND status='active')<>1
    OR NOT EXISTS (
      SELECT 1 FROM transfer_existing_source_evidence AS evidence
       WHERE evidence.student_id=NEW.student_id
         AND evidence.current_enrolment_id=NEW.current_enrolment_id
         AND evidence.ordinary_relationship_cycle_id=NEW.ordinary_relationship_cycle_id
         AND evidence.ordinary_relationship_contract_id IS NEW.ordinary_relationship_contract_id
         AND evidence.private_school_application_id IS NEW.private_school_application_id
         AND evidence.public_application_group_id IS NEW.public_application_group_id
    )
  ) THEN RAISE(ABORT,'Transfer Existing authority requires exact verified dual evidence') END;
END;

CREATE TRIGGER transfer_source_authority_validate_update_041
BEFORE UPDATE ON transfer_source_authorities
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM contracts WHERE id=NEW.transfer_contract_id
      AND student_id=NEW.student_id AND contract_type='TransferApplicationContract'
      AND deleted_at IS NULL AND voided_at IS NULL
  ) THEN RAISE(ABORT,'Transfer source contract ownership mismatch') END;
  SELECT CASE WHEN NEW.parent_form_token IS NOT NULL AND (
    NOT EXISTS (SELECT 1 FROM parent_form_tokens WHERE token=NEW.parent_form_token
      AND student_id=NEW.student_id AND intake_type='new_student_transfer'
      AND invalidated_at IS NULL AND length(trim(COALESCE(submitted_at,'')))>0)
    OR EXISTS (SELECT 1 FROM transfer_existing_source_evidence WHERE student_id=NEW.student_id)
  ) THEN RAISE(ABORT,'Transfer new intake authority missing or conflicting') END;
  SELECT CASE WHEN NEW.parent_form_token IS NULL AND (
    (SELECT count(*) FROM student_school_enrolments WHERE student_id=NEW.student_id
      AND enrolment_role='current' AND status='active')<>1
    OR NOT EXISTS (
      SELECT 1 FROM transfer_existing_source_evidence AS evidence
       WHERE evidence.student_id=NEW.student_id
         AND evidence.current_enrolment_id=NEW.current_enrolment_id
         AND evidence.ordinary_relationship_cycle_id=NEW.ordinary_relationship_cycle_id
         AND evidence.ordinary_relationship_contract_id IS NEW.ordinary_relationship_contract_id
         AND evidence.private_school_application_id IS NEW.private_school_application_id
         AND evidence.public_application_group_id IS NEW.public_application_group_id
    )
  ) THEN RAISE(ABORT,'Transfer Existing authority requires exact verified dual evidence') END;
END;

CREATE TRIGGER transfer_source_authority_consume_041
BEFORE UPDATE ON transfer_source_authorities
WHEN OLD.lifecycle_state='verified' AND NEW.lifecycle_state='consumed'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM contracts AS contract
    JOIN application_cycles AS cycle ON cycle.id=NEW.application_cycle_id
    JOIN transfer_cases AS transfer ON transfer.case_id=cycle.source_student_case_id
    JOIN student_cases AS student_case ON student_case.id=transfer.case_id
    WHERE contract.id=NEW.transfer_contract_id
      AND contract.contract_status='FullyExecuted'
      AND contract.application_cycle_id=cycle.id
      AND cycle.student_id=NEW.student_id AND cycle.cycle_type='transfer'
      AND cycle.education_stream='school_education'
      AND student_case.student_id=NEW.student_id AND student_case.case_type='transfer'
      AND transfer.source_contract_id=contract.id
      AND transfer.transfer_source_label=NEW.source_classification
  ) THEN RAISE(ABORT,'Transfer source consumption requires exact executed contract and Cycle projection') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM transfer_v2_origin_events AS origin
    WHERE origin.application_cycle_id=NEW.application_cycle_id
      AND origin.new_source_label=NEW.source_classification
      AND origin.id=(SELECT max(id) FROM transfer_v2_origin_events WHERE application_cycle_id=NEW.application_cycle_id)
  ) THEN RAISE(ABORT,'Transfer source consumption requires matching origin projection') END;
END;

CREATE TRIGGER transfer_source_parent_intake_guard_041
BEFORE UPDATE OF student_id,intake_type ON parent_form_tokens
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.intake_type IS NOT OLD.intake_type)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE parent_form_token=OLD.token AND lifecycle_state IN ('verified','consumed'))
BEGIN SELECT RAISE(ABORT,'Consumed Transfer Parent intake evidence is immutable'); END;

CREATE TRIGGER transfer_source_case_projection_guard_041
BEFORE UPDATE OF transfer_source_label,source_contract_id ON transfer_cases
WHEN EXISTS (
 SELECT 1 FROM transfer_source_authorities AS authority
 JOIN application_cycles AS cycle ON cycle.id=authority.application_cycle_id
 WHERE authority.lifecycle_state='consumed' AND cycle.source_student_case_id=OLD.case_id
   AND (NEW.transfer_source_label IS NOT authority.source_classification
        OR NEW.source_contract_id IS NOT authority.transfer_contract_id)
)
BEGIN SELECT RAISE(ABORT,'Transfer source projection cannot drift'); END;

CREATE TRIGGER transfer_source_origin_projection_guard_041
BEFORE INSERT ON transfer_v2_origin_events
WHEN EXISTS (
 SELECT 1 FROM transfer_source_authorities WHERE application_cycle_id=NEW.application_cycle_id
  AND lifecycle_state='consumed' AND source_classification IS NOT NEW.new_source_label
)
BEGIN SELECT RAISE(ABORT,'Transfer source origin cannot drift'); END;

CREATE TRIGGER transfer_source_enrolment_identity_guard_041
BEFORE UPDATE OF student_id ON student_school_enrolments
WHEN (NEW.student_id IS NOT OLD.student_id)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE current_enrolment_id=OLD.id AND lifecycle_state IN ('verified','consumed'))
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

CREATE TRIGGER transfer_source_ordinary_cycle_identity_guard_041
BEFORE UPDATE OF student_id,cycle_type,education_stream ON application_cycles
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.cycle_type IS NOT OLD.cycle_type OR NEW.education_stream IS NOT OLD.education_stream)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE ordinary_relationship_cycle_id=OLD.id AND lifecycle_state IN ('verified','consumed'))
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

CREATE TRIGGER transfer_source_ordinary_contract_identity_guard_041
BEFORE UPDATE OF student_id,contract_type,application_cycle_id ON contracts
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.contract_type IS NOT OLD.contract_type OR NEW.application_cycle_id IS NOT OLD.application_cycle_id)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE ordinary_relationship_contract_id=OLD.id AND lifecycle_state IN ('verified','consumed'))
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

CREATE TRIGGER transfer_source_private_application_identity_guard_041
BEFORE UPDATE OF student_id,application_cycle_id ON private_school_applications
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.application_cycle_id IS NOT OLD.application_cycle_id)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE private_school_application_id=OLD.id AND lifecycle_state IN ('verified','consumed'))
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

CREATE TRIGGER transfer_source_public_application_identity_guard_041
BEFORE UPDATE OF student_id,application_cycle_id ON public_application_groups
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.application_cycle_id IS NOT OLD.application_cycle_id)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE public_application_group_id=OLD.id AND lifecycle_state IN ('verified','consumed'))
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

CREATE TRIGGER transfer_source_transfer_cycle_identity_guard_041
BEFORE UPDATE OF student_id,cycle_type,education_stream,source_student_case_id ON application_cycles
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.cycle_type IS NOT OLD.cycle_type OR NEW.education_stream IS NOT OLD.education_stream OR NEW.source_student_case_id IS NOT OLD.source_student_case_id)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE application_cycle_id=OLD.id AND lifecycle_state='consumed')
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

CREATE TRIGGER transfer_source_transfer_contract_identity_guard_041
BEFORE UPDATE OF student_id,contract_type,application_cycle_id ON contracts
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.contract_type IS NOT OLD.contract_type OR NEW.application_cycle_id IS NOT OLD.application_cycle_id)
 AND EXISTS(SELECT 1 FROM transfer_source_authorities WHERE transfer_contract_id=OLD.id AND lifecycle_state='consumed')
BEGIN SELECT RAISE(ABORT,'Consumed Transfer evidence identity is immutable'); END;

-- REPLACE may suppress DELETE triggers with recursive_triggers=OFF (SQLite default).
-- Reject replacement before its implicit delete; do not rely on FK side effects.
CREATE TRIGGER transfer_source_authority_no_replace_041
BEFORE INSERT ON transfer_source_authorities
WHEN EXISTS(SELECT 1 FROM transfer_source_authorities
 WHERE id=NEW.id OR transfer_contract_id=NEW.transfer_contract_id)
BEGIN SELECT RAISE(ABORT,'Transfer source authority cannot be replaced'); END;

CREATE TRIGGER transfer_source_origin_no_replace_041
BEFORE INSERT ON transfer_v2_origin_events
WHEN EXISTS(
 SELECT 1 FROM transfer_v2_origin_events AS old_origin
 JOIN transfer_source_authorities AS authority
   ON authority.application_cycle_id=old_origin.application_cycle_id
 WHERE authority.lifecycle_state='consumed'
   AND (old_origin.id=NEW.id OR old_origin.event_uid=NEW.event_uid)
)
BEGIN SELECT RAISE(ABORT,'Consumed Transfer source origin cannot be replaced'); END;

CREATE TRIGGER transfer_source_student_case_identity_guard_041
BEFORE UPDATE OF student_id,case_type ON student_cases
WHEN (NEW.student_id IS NOT OLD.student_id OR NEW.case_type IS NOT OLD.case_type)
 AND EXISTS(
 SELECT 1 FROM transfer_source_authorities AS authority
 JOIN application_cycles AS cycle ON cycle.id=authority.application_cycle_id
 WHERE authority.lifecycle_state='consumed' AND cycle.source_student_case_id=OLD.id
 )
BEGIN SELECT RAISE(ABORT,'Consumed Transfer student Case identity is immutable'); END;

CREATE TRIGGER transfer_source_case_insert_projection_guard_041
BEFORE INSERT ON transfer_cases
WHEN EXISTS(
 SELECT 1 FROM transfer_source_authorities AS authority
 JOIN application_cycles AS cycle ON cycle.id=authority.application_cycle_id
 WHERE authority.lifecycle_state='consumed' AND cycle.source_student_case_id=NEW.case_id
   AND (NEW.transfer_source_label IS NOT authority.source_classification
     OR NEW.source_contract_id IS NOT authority.transfer_contract_id)
)
BEGIN SELECT RAISE(ABORT,'Transfer source projection cannot drift through replacement'); END;
