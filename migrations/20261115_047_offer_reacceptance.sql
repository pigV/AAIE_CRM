-- migration_mode: sqlite_fk_off_rebuild
-- Historical rounds can share an Offer receipt. Keep one active round per cycle.

CREATE TABLE cycle_final_acceptances_rebuild_047 (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  offer_received_event_id INTEGER NOT NULL
    REFERENCES offer_received_events(id) ON DELETE RESTRICT,
  school_id INTEGER,
  school_name_snapshot TEXT NOT NULL,
  signed_offer_completed_at TEXT,
  payment_completed_at TEXT,
  selected_at TEXT NOT NULL,
  selected_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  selected_by_name TEXT,
  acceptance_status TEXT NOT NULL DEFAULT 'active'
    CHECK(acceptance_status IN ('active','withdrawn','superseded')),
  ended_at TEXT,
  ended_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  ended_by_name TEXT,
  status_reason TEXT,
  replaces_acceptance_id INTEGER
    REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  superseded_by_acceptance_id INTEGER
    REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, transfer_branch_id INTEGER
  REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  CHECK(
    (acceptance_status='active' AND ended_at IS NULL
      AND ended_by_user_id IS NULL AND ended_by_name IS NULL
      AND superseded_by_acceptance_id IS NULL)
    OR
    (acceptance_status IN ('withdrawn','superseded') AND ended_at IS NOT NULL)
  ),
  CHECK(replaces_acceptance_id IS NULL OR replaces_acceptance_id<>id),
  CHECK(superseded_by_acceptance_id IS NULL OR superseded_by_acceptance_id<>id)
);

INSERT INTO cycle_final_acceptances_rebuild_047(
  id,application_cycle_id,student_id,offer_received_event_id,
  school_id,school_name_snapshot,signed_offer_completed_at,payment_completed_at,
  selected_at,selected_by_user_id,selected_by_name,acceptance_status,
  ended_at,ended_by_user_id,ended_by_name,status_reason,
  replaces_acceptance_id,superseded_by_acceptance_id,created_at,updated_at,
  transfer_branch_id
)
SELECT
  id,application_cycle_id,student_id,offer_received_event_id,
  school_id,school_name_snapshot,signed_offer_completed_at,payment_completed_at,
  selected_at,selected_by_user_id,selected_by_name,acceptance_status,
  ended_at,ended_by_user_id,ended_by_name,status_reason,
  replaces_acceptance_id,superseded_by_acceptance_id,created_at,updated_at,
  transfer_branch_id
FROM cycle_final_acceptances;

DROP TABLE cycle_final_acceptances;
ALTER TABLE cycle_final_acceptances_rebuild_047 RENAME TO cycle_final_acceptances;

CREATE INDEX idx_cycle_final_acceptances_offer
ON cycle_final_acceptances(offer_received_event_id);

CREATE INDEX idx_cycle_final_acceptances_cycle_status
ON cycle_final_acceptances(application_cycle_id,acceptance_status,selected_at,id);

CREATE INDEX idx_cycle_final_acceptances_transfer_branch
ON cycle_final_acceptances(transfer_branch_id,acceptance_status,id)
WHERE transfer_branch_id IS NOT NULL;

CREATE UNIQUE INDEX uq_cycle_final_acceptances_one_active
ON cycle_final_acceptances(application_cycle_id)
WHERE acceptance_status='active';

CREATE TRIGGER cycle_final_acceptances_identity_immutable
BEFORE UPDATE OF application_cycle_id,student_id,offer_received_event_id,
  school_id,school_name_snapshot,selected_at,selected_by_user_id,selected_by_name,
  replaces_acceptance_id
ON cycle_final_acceptances
BEGIN
  SELECT RAISE(ABORT, 'final acceptance identity is immutable');
END;

CREATE TRIGGER cycle_final_acceptances_no_delete
BEFORE DELETE ON cycle_final_acceptances
BEGIN
  SELECT RAISE(ABORT, 'final acceptances are retained permanently');
END;

CREATE TRIGGER cycle_final_acceptances_ownership_insert
BEFORE INSERT ON cycle_final_acceptances
WHEN NOT EXISTS (
  SELECT 1 FROM offer_received_events
  WHERE id=NEW.offer_received_event_id
    AND application_cycle_id=NEW.application_cycle_id
    AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'final acceptance ownership mismatch');
END;

CREATE TRIGGER transfer_v2_acceptance_branch_insert
BEFORE INSERT ON cycle_final_acceptances
WHEN NEW.transfer_branch_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM offer_received_events AS offer_event
  WHERE offer_event.id=NEW.offer_received_event_id
    AND offer_event.application_cycle_id=NEW.application_cycle_id
    AND offer_event.transfer_branch_id=NEW.transfer_branch_id
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 acceptance ownership mismatch');
END;

CREATE TRIGGER transfer_v2_acceptance_branch_update
BEFORE UPDATE OF application_cycle_id,offer_received_event_id,transfer_branch_id
ON cycle_final_acceptances
WHEN NEW.transfer_branch_id IS NOT NULL AND NOT EXISTS (
  SELECT 1
  FROM offer_received_events AS offer_event
  WHERE offer_event.id=NEW.offer_received_event_id
    AND offer_event.application_cycle_id=NEW.application_cycle_id
    AND offer_event.transfer_branch_id=NEW.transfer_branch_id
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 acceptance ownership mismatch');
END;
