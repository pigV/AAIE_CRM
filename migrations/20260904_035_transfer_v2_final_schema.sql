-- migration_mode: sqlite_fk_off_rebuild
-- Transfer V2 final approved schema. No backfill and no historical fact rewrite.

ALTER TABLE parent_form_tokens ADD COLUMN intake_type TEXT
  CHECK(intake_type IS NULL OR intake_type IN (
    'ordinary_new_application','new_student_transfer'
  ));

CREATE TABLE parent_form_intake_type_change_events (
  id INTEGER PRIMARY KEY,
  parent_form_token TEXT NOT NULL
    REFERENCES parent_form_tokens(token) ON DELETE RESTRICT,
  previous_intake_type TEXT NOT NULL CHECK(previous_intake_type IN (
    'ordinary_new_application','new_student_transfer'
  )),
  new_intake_type TEXT NOT NULL CHECK(new_intake_type IN (
    'ordinary_new_application','new_student_transfer'
  )),
  changed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  changed_at TEXT NOT NULL CHECK(length(trim(changed_at))>0),
  CHECK(previous_intake_type<>new_intake_type)
);

CREATE INDEX idx_parent_form_intake_change_token
ON parent_form_intake_type_change_events(parent_form_token,changed_at,id);

CREATE TRIGGER parent_form_tokens_intake_required_insert_035
BEFORE INSERT ON parent_form_tokens
WHEN NEW.intake_type IS NULL
BEGIN
  SELECT RAISE(ABORT,'Parent Link intake type is required');
END;

CREATE TRIGGER parent_form_intake_change_validate_035
BEFORE INSERT ON parent_form_intake_type_change_events
WHEN NOT EXISTS (
  SELECT 1 FROM parent_form_tokens AS token
   WHERE token.token=NEW.parent_form_token
     AND token.intake_type=NEW.previous_intake_type
     AND token.intake_type IS NOT NULL
)
OR EXISTS (
  SELECT 1 FROM application_cycles AS cycle
   WHERE cycle.parent_form_token=NEW.parent_form_token
     AND (
       cycle.cycle_type<>'initial'
       OR cycle.status<>'active'
       OR cycle.source_student_case_id IS NOT NULL
     )
)
OR EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN contracts AS contract ON contract.application_cycle_id=cycle.id
   WHERE cycle.parent_form_token=NEW.parent_form_token
     AND contract.contract_type='LocalStudentApplicationContract'
     AND contract.contract_status='FullyExecuted'
     AND contract.deleted_at IS NULL
     AND contract.voided_at IS NULL
)
OR EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN private_school_applications AS application
      ON application.application_cycle_id=cycle.id
   WHERE cycle.parent_form_token=NEW.parent_form_token
     AND application.status<>'not_submitted'
)
OR EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN public_application_groups AS application
      ON application.application_cycle_id=cycle.id
   WHERE cycle.parent_form_token=NEW.parent_form_token
     AND application.status<>'draft'
)
OR EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN offer_received_events AS offer ON offer.application_cycle_id=cycle.id
   WHERE cycle.parent_form_token=NEW.parent_form_token
)
OR EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN cycle_final_acceptances AS acceptance
      ON acceptance.application_cycle_id=cycle.id
   WHERE cycle.parent_form_token=NEW.parent_form_token
)
OR EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN coe_oshc AS coe ON coe.application_cycle_id=cycle.id
   WHERE cycle.parent_form_token=NEW.parent_form_token
)
OR EXISTS (
  SELECT 1
    FROM parent_form_tokens AS token
    JOIN application_cycles AS transfer_cycle
      ON transfer_cycle.student_id=token.student_id
   WHERE token.token=NEW.parent_form_token
     AND transfer_cycle.cycle_type='transfer'
     AND transfer_cycle.status='active'
)
BEGIN
  SELECT RAISE(ABORT,'Parent Link intake correction gate rejected');
END;

CREATE TRIGGER parent_form_intake_change_apply_035
AFTER INSERT ON parent_form_intake_type_change_events
BEGIN
  UPDATE parent_form_tokens
     SET intake_type=NEW.new_intake_type
   WHERE token=NEW.parent_form_token
     AND intake_type=NEW.previous_intake_type;
END;

CREATE TRIGGER parent_form_tokens_intake_update_guard_035
BEFORE UPDATE OF intake_type ON parent_form_tokens
WHEN NEW.intake_type IS NOT OLD.intake_type
 AND NOT EXISTS (
   SELECT 1
     FROM parent_form_intake_type_change_events AS event
    WHERE event.id=(
      SELECT max(latest.id)
        FROM parent_form_intake_type_change_events AS latest
       WHERE latest.parent_form_token=OLD.token
    )
      AND event.parent_form_token=OLD.token
      AND event.previous_intake_type=OLD.intake_type
      AND event.new_intake_type=NEW.intake_type
 )
BEGIN
  SELECT RAISE(ABORT,'Parent Link intake type requires a correction event');
END;

CREATE TRIGGER parent_form_intake_change_no_update_035
BEFORE UPDATE ON parent_form_intake_type_change_events BEGIN
  SELECT RAISE(ABORT,'Parent Link intake correction events are append-only');
END;

CREATE TRIGGER parent_form_intake_change_no_delete_035
BEFORE DELETE ON parent_form_intake_type_change_events BEGIN
  SELECT RAISE(ABORT,'Parent Link intake correction events are append-only');
END;

ALTER TABLE transfer_school_applications ADD COLUMN removed_at TEXT;
ALTER TABLE transfer_school_applications ADD COLUMN removed_by INTEGER
  REFERENCES users(id) ON DELETE RESTRICT;

DROP TRIGGER transfer_target_state_events_no_update;
DROP TRIGGER transfer_target_state_events_no_delete;
DROP INDEX idx_transfer_target_state_current;

CREATE TABLE transfer_target_state_events_rebuild_035 (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL
    REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  previous_state TEXT CHECK(previous_state IS NULL OR previous_state IN (
    'materials_preparation','application_in_progress','accepted','paid','coe','completed',
    'rejected','withdrawn','application_submitted','awaiting_interview',
    'awaiting_result','interview_failed','school_full','grades_insufficient',
    'parent_withdrawn','other','offer','closed_after_transfer_completion'
  )),
  new_state TEXT NOT NULL CHECK(new_state IN (
    'materials_preparation','application_in_progress','accepted','paid','coe','completed',
    'rejected','withdrawn','application_submitted','awaiting_interview',
    'awaiting_result','interview_failed','school_full','grades_insufficient',
    'parent_withdrawn','other','offer','closed_after_transfer_completion'
  )),
  event_type TEXT NOT NULL CHECK(length(trim(event_type))>0),
  missing_checklist_json TEXT,
  note TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);

INSERT INTO transfer_target_state_events_rebuild_035(
  id,event_uid,application_cycle_id,branch_id,previous_state,new_state,event_type,
  missing_checklist_json,note,actor_user_id,occurred_at,created_at
)
SELECT id,event_uid,application_cycle_id,branch_id,previous_state,new_state,event_type,
       missing_checklist_json,note,actor_user_id,occurred_at,created_at
  FROM transfer_target_state_events;

DROP TABLE transfer_target_state_events;
ALTER TABLE transfer_target_state_events_rebuild_035 RENAME TO transfer_target_state_events;

CREATE INDEX idx_transfer_target_state_current
ON transfer_target_state_events(branch_id,occurred_at DESC,id DESC);

CREATE TRIGGER transfer_target_state_events_no_update
BEFORE UPDATE ON transfer_target_state_events BEGIN
  SELECT RAISE(ABORT,'Transfer target state events are append-only');
END;

CREATE TRIGGER transfer_target_state_events_no_delete
BEFORE DELETE ON transfer_target_state_events BEGIN
  SELECT RAISE(ABORT,'Transfer target state events are append-only');
END;

CREATE TRIGGER transfer_target_state_new_cycle_validate_035
BEFORE INSERT ON transfer_target_state_events
WHEN EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN schema_migrations AS cutover ON cutover.version='20260904_035'
   WHERE cycle.id=NEW.application_cycle_id
     AND cycle.created_at>=cutover.applied_at
)
AND (
  NEW.new_state NOT IN (
    'application_submitted','awaiting_interview','awaiting_result','interview_failed',
    'school_full','grades_insufficient','parent_withdrawn','other','offer',
    'closed_after_transfer_completion'
  )
  OR (NEW.new_state='other' AND length(trim(COALESCE(NEW.note,'')))=0)
  OR (NEW.new_state='offer' AND NEW.event_type<>'offer_confirmed')
  OR (NEW.new_state='closed_after_transfer_completion'
      AND NEW.event_type<>'transfer_completed')
)
BEGIN
  SELECT RAISE(ABORT,'Transfer target state is not allowed for a new Cycle');
END;

CREATE TRIGGER transfer_target_state_legacy_read_only_035
BEFORE INSERT ON transfer_target_state_events
WHEN EXISTS (
  SELECT 1
    FROM application_cycles AS cycle
    JOIN schema_migrations AS cutover ON cutover.version='20260904_035'
   WHERE cycle.id=NEW.application_cycle_id
     AND cycle.created_at<cutover.applied_at
)
BEGIN
  SELECT RAISE(ABORT,'Legacy Transfer target state is read only');
END;

CREATE TRIGGER transfer_target_state_branch_validate_035
BEFORE INSERT ON transfer_target_state_events
WHEN NOT EXISTS (
  SELECT 1 FROM transfer_school_applications AS branch
   WHERE branch.id=NEW.branch_id
     AND branch.application_cycle_id=NEW.application_cycle_id
     AND branch.removed_at IS NULL
)
OR NEW.previous_state IS NOT (
  SELECT state.new_state
    FROM transfer_target_state_events AS state
   WHERE state.branch_id=NEW.branch_id
   ORDER BY state.occurred_at DESC,state.id DESC LIMIT 1
)
BEGIN
  SELECT RAISE(ABORT,'Transfer target state ownership or continuity mismatch');
END;

CREATE TRIGGER transfer_target_offer_authority_035
BEFORE INSERT ON transfer_target_state_events
WHEN NEW.new_state='offer' AND NOT EXISTS (
  SELECT 1
    FROM offer_received_events AS offer
    JOIN offer_received_event_state_events AS state
      ON state.offer_received_event_id=offer.id
   WHERE offer.application_cycle_id=NEW.application_cycle_id
     AND offer.transfer_branch_id=NEW.branch_id
     AND state.id=(
       SELECT max(latest.id) FROM offer_received_event_state_events AS latest
        WHERE latest.offer_received_event_id=offer.id
     )
     AND state.state='ACTIVE'
)
BEGIN
  SELECT RAISE(ABORT,'Transfer target Offer requires formal Offer confirmation');
END;

CREATE INDEX idx_transfer_target_active_school_035
ON transfer_school_applications(application_cycle_id,school_id,school_name_snapshot)
WHERE removed_at IS NULL;

CREATE TRIGGER transfer_target_active_duplicate_insert_035
BEFORE INSERT ON transfer_school_applications
WHEN NEW.application_cycle_id IS NOT NULL AND EXISTS (
  SELECT 1
    FROM transfer_school_applications AS active
   WHERE active.application_cycle_id=NEW.application_cycle_id
     AND active.removed_at IS NULL
     AND (
       (active.school_id IS NOT NULL AND NEW.school_id IS NOT NULL
        AND active.school_id=NEW.school_id)
       OR
       ((active.school_id IS NULL OR NEW.school_id IS NULL)
        AND lower(trim(active.school_name_snapshot))=lower(trim(NEW.school_name_snapshot))
        AND lower(trim(COALESCE(active.school_state_snapshot,'')))=
            lower(trim(COALESCE(NEW.school_state_snapshot,''))))
     )
)
BEGIN
  SELECT RAISE(ABORT,'Only one active Target School is allowed per Cycle and school');
END;

CREATE TRIGGER transfer_target_active_duplicate_update_035
BEFORE UPDATE OF school_id,school_name_snapshot,school_state_snapshot,removed_at
ON transfer_school_applications
WHEN NEW.removed_at IS NULL AND NEW.application_cycle_id IS NOT NULL AND EXISTS (
  SELECT 1
    FROM transfer_school_applications AS active
   WHERE active.id<>OLD.id
     AND active.application_cycle_id=NEW.application_cycle_id
     AND active.removed_at IS NULL
     AND (
       (active.school_id IS NOT NULL AND NEW.school_id IS NOT NULL
        AND active.school_id=NEW.school_id)
       OR
       ((active.school_id IS NULL OR NEW.school_id IS NULL)
        AND lower(trim(active.school_name_snapshot))=lower(trim(NEW.school_name_snapshot))
        AND lower(trim(COALESCE(active.school_state_snapshot,'')))=
            lower(trim(COALESCE(NEW.school_state_snapshot,''))))
     )
)
BEGIN
  SELECT RAISE(ABORT,'Only one active Target School is allowed per Cycle and school');
END;

CREATE TRIGGER transfer_target_removal_validate_035
BEFORE UPDATE OF removed_at,removed_by ON transfer_school_applications
WHEN OLD.removed_at IS NULL AND NEW.removed_at IS NOT NULL
AND (
  NEW.removed_by IS NULL
  OR NEW.is_final=1
  OR EXISTS (
    SELECT 1 FROM transfer_target_state_events AS state
     WHERE state.branch_id=OLD.id
       AND state.new_state IN ('application_in_progress','application_submitted')
  )
  OR EXISTS (
    SELECT 1 FROM case_record_links AS link
     WHERE link.transfer_application_id=OLD.id
       AND link.record_type IN ('offer','payment','coe','confirmation_letter')
  )
)
BEGIN
  SELECT RAISE(ABORT,'A submitted target school cannot be removed');
END;

CREATE TRIGGER transfer_target_removal_pair_035
BEFORE UPDATE OF removed_at,removed_by ON transfer_school_applications
WHEN (NEW.removed_at IS NULL)<>(NEW.removed_by IS NULL)
BEGIN
  SELECT RAISE(ABORT,'Target School removal actor and time are required together');
END;

CREATE TRIGGER transfer_target_removal_immutable_035
BEFORE UPDATE OF removed_at,removed_by ON transfer_school_applications
WHEN OLD.removed_at IS NOT NULL
 AND (NEW.removed_at IS NOT OLD.removed_at OR NEW.removed_by IS NOT OLD.removed_by)
BEGIN
  SELECT RAISE(ABORT,'Target School removal is immutable');
END;

CREATE TRIGGER transfer_target_identity_submitted_immutable_035
BEFORE UPDATE OF school_id,school_name_snapshot,school_state_snapshot
ON transfer_school_applications
WHEN EXISTS (
  SELECT 1 FROM transfer_target_state_events AS state
   WHERE state.branch_id=OLD.id
     AND state.new_state IN ('application_in_progress','application_submitted')
)
BEGIN
  SELECT RAISE(ABORT,'Submitted Target School identity is immutable');
END;

CREATE TABLE transfer_payment_confirmations (
  id INTEGER PRIMARY KEY,
  payment_link_id INTEGER NOT NULL UNIQUE
    REFERENCES case_record_links(id) ON DELETE RESTRICT,
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL CHECK(length(trim(confirmed_at))>0)
);

CREATE TRIGGER transfer_payment_confirmation_validate_035
BEFORE INSERT ON transfer_payment_confirmations
WHEN NOT EXISTS (
  SELECT 1
    FROM case_record_links AS link
    JOIN transfer_school_applications AS branch
      ON branch.id=link.transfer_application_id
    JOIN transfer_cases AS transfer_case ON transfer_case.case_id=branch.case_id
    JOIN application_cycles AS cycle
      ON cycle.id=branch.application_cycle_id
     AND cycle.source_student_case_id=transfer_case.case_id
    JOIN payments AS payment ON payment.id=link.record_id
    JOIN schema_migrations AS cutover ON cutover.version='20260904_035'
   WHERE link.id=NEW.payment_link_id
     AND link.record_type='payment'
     AND link.validity_status='current'
     AND branch.removed_at IS NULL
     AND cycle.cycle_type='transfer'
     AND cycle.created_at>=cutover.applied_at
     AND payment.student_id=cycle.student_id
     AND payment.amount>0
     AND length(trim(COALESCE(payment.paid_date,'')))>0
     AND length(trim(COALESCE(payment.receipt_file_path,'')))>0
)
BEGIN
  SELECT RAISE(ABORT,'Transfer Payment confirmation authority mismatch');
END;

CREATE TRIGGER transfer_payment_confirmations_no_update_035
BEFORE UPDATE ON transfer_payment_confirmations BEGIN
  SELECT RAISE(ABORT,'Transfer Payment confirmations are append-only');
END;

CREATE TRIGGER transfer_payment_confirmations_no_delete_035
BEFORE DELETE ON transfer_payment_confirmations BEGIN
  SELECT RAISE(ABORT,'Transfer Payment confirmations are append-only');
END;
