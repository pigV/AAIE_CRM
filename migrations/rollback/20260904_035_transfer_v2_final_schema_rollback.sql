-- Disposable rehearsal rollback only. Run only before any post-cutover facts exist.
DROP TRIGGER IF EXISTS transfer_payment_confirmations_no_delete_035;
DROP TRIGGER IF EXISTS transfer_payment_confirmations_no_update_035;
DROP TRIGGER IF EXISTS transfer_payment_confirmation_validate_035;
DROP TABLE transfer_payment_confirmations;

DROP TRIGGER IF EXISTS transfer_target_identity_submitted_immutable_035;
DROP TRIGGER IF EXISTS transfer_target_removal_immutable_035;
DROP TRIGGER IF EXISTS transfer_target_removal_pair_035;
DROP TRIGGER IF EXISTS transfer_target_removal_validate_035;
DROP TRIGGER IF EXISTS transfer_target_active_duplicate_update_035;
DROP TRIGGER IF EXISTS transfer_target_active_duplicate_insert_035;
DROP INDEX IF EXISTS idx_transfer_target_active_school_035;
DROP TRIGGER IF EXISTS transfer_target_offer_authority_035;
DROP TRIGGER IF EXISTS transfer_target_state_branch_validate_035;
DROP TRIGGER IF EXISTS transfer_target_state_legacy_read_only_035;
DROP TRIGGER IF EXISTS transfer_target_state_new_cycle_validate_035;

DROP TRIGGER transfer_target_state_events_no_update;
DROP TRIGGER transfer_target_state_events_no_delete;
DROP INDEX idx_transfer_target_state_current;
CREATE TABLE transfer_target_state_events_rollback_035 (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  previous_state TEXT CHECK(previous_state IS NULL OR previous_state IN (
    'materials_preparation','application_in_progress','offer','accepted','paid',
    'coe','completed','rejected','withdrawn','closed_after_transfer_completion'
  )),
  new_state TEXT NOT NULL CHECK(new_state IN (
    'materials_preparation','application_in_progress','offer','accepted','paid',
    'coe','completed','rejected','withdrawn','closed_after_transfer_completion'
  )),
  event_type TEXT NOT NULL CHECK(length(trim(event_type))>0),
  missing_checklist_json TEXT,
  note TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);
INSERT INTO transfer_target_state_events_rollback_035
SELECT * FROM transfer_target_state_events;
DROP TABLE transfer_target_state_events;
ALTER TABLE transfer_target_state_events_rollback_035 RENAME TO transfer_target_state_events;
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

ALTER TABLE transfer_school_applications DROP COLUMN removed_by;
ALTER TABLE transfer_school_applications DROP COLUMN removed_at;

DROP TRIGGER IF EXISTS parent_form_intake_change_no_delete_035;
DROP TRIGGER IF EXISTS parent_form_intake_change_no_update_035;
DROP TRIGGER IF EXISTS parent_form_tokens_intake_update_guard_035;
DROP TRIGGER IF EXISTS parent_form_intake_change_apply_035;
DROP TRIGGER IF EXISTS parent_form_intake_change_validate_035;
DROP TRIGGER IF EXISTS parent_form_tokens_intake_required_insert_035;
DROP INDEX IF EXISTS idx_parent_form_intake_change_token;
DROP TABLE parent_form_intake_type_change_events;
ALTER TABLE parent_form_tokens DROP COLUMN intake_type;
