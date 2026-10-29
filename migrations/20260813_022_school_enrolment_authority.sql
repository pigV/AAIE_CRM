-- Additive R5 delta: preserve the existing enrolment rows and unique slot indexes.

ALTER TABLE student_school_enrolments ADD COLUMN application_cycle_id INTEGER
  REFERENCES application_cycles(id) ON DELETE RESTRICT;
ALTER TABLE student_school_enrolments ADD COLUMN year_level_snapshot TEXT;
ALTER TABLE student_school_enrolments ADD COLUMN lifecycle_context TEXT
  CHECK(lifecycle_context IN ('initial','transfer','progression','legacy'));
ALTER TABLE student_school_enrolments ADD COLUMN confirmation_reason TEXT;
ALTER TABLE student_school_enrolments ADD COLUMN confirmed_by INTEGER
  REFERENCES users(id) ON DELETE RESTRICT;
ALTER TABLE student_school_enrolments ADD COLUMN confirmed_at TEXT;

CREATE TABLE school_enrolment_transition_intents (
  id INTEGER PRIMARY KEY,
  intent_uid TEXT NOT NULL UNIQUE CHECK(length(trim(intent_uid)) > 0),
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  source_case_id INTEGER REFERENCES student_cases(id) ON DELETE RESTRICT,
  application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT,
  current_enrolment_id INTEGER NOT NULL
    REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
  upcoming_enrolment_id INTEGER NOT NULL
    REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
  effective_date TEXT NOT NULL CHECK(date(effective_date)=effective_date),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','applied','cancelled','reversed')),
  reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  applied_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  applied_at TEXT,
  cancelled_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  cancelled_at TEXT,
  cancellation_reason TEXT,
  reversed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  reversed_at TEXT,
  reversal_reason TEXT,
  CHECK(current_enrolment_id <> upcoming_enrolment_id),
  CHECK(
    (status='pending' AND applied_at IS NULL AND cancelled_at IS NULL
      AND reversed_at IS NULL)
    OR (status='applied' AND applied_by IS NOT NULL AND applied_at IS NOT NULL
      AND cancelled_at IS NULL AND reversed_at IS NULL)
    OR (status='cancelled' AND cancelled_by IS NOT NULL
      AND cancelled_at IS NOT NULL AND length(trim(cancellation_reason)) > 0
      AND applied_at IS NULL AND reversed_at IS NULL)
    OR (status='reversed' AND applied_by IS NOT NULL AND applied_at IS NOT NULL
      AND reversed_by IS NOT NULL AND reversed_at IS NOT NULL
      AND length(trim(reversal_reason)) > 0)
  )
);

CREATE UNIQUE INDEX uq_school_enrolment_transition_pending_upcoming
ON school_enrolment_transition_intents(upcoming_enrolment_id)
WHERE status='pending';

CREATE INDEX ix_school_enrolment_transition_due
ON school_enrolment_transition_intents(status,effective_date,id);

CREATE TABLE school_enrolment_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) > 0),
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  source_case_id INTEGER REFERENCES student_cases(id) ON DELETE RESTRICT,
  application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT,
  transition_intent_id INTEGER
    REFERENCES school_enrolment_transition_intents(id) ON DELETE RESTRICT,
  enrolment_id INTEGER REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL CHECK(event_type IN (
    'current_confirmed','upcoming_confirmed','transition_scheduled',
    'transition_applied','upcoming_cancelled','transition_reversed',
    'enrolment_corrected'
  )),
  old_value_json TEXT,
  new_value_json TEXT,
  reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL
);

CREATE INDEX ix_school_enrolment_events_student
ON school_enrolment_events(student_id,occurred_at,id);

CREATE TRIGGER student_school_enrolments_lifecycle_ownership_insert
BEFORE INSERT ON student_school_enrolments
WHEN (NEW.source_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
   WHERE id=NEW.source_case_id AND student_id=NEW.student_id
)) OR (NEW.application_cycle_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM application_cycles
   WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
     AND (NEW.source_case_id IS NULL OR source_student_case_id=NEW.source_case_id)
     AND (NEW.lifecycle_context IS NULL OR cycle_type=NEW.lifecycle_context)
)) OR ((NEW.confirmed_by IS NULL OR NEW.confirmed_at IS NULL
         OR NEW.confirmation_reason IS NULL)
        AND NOT (NEW.confirmed_by IS NULL AND NEW.confirmed_at IS NULL
                 AND NEW.confirmation_reason IS NULL))
BEGIN
  SELECT RAISE(ABORT,'school enrolment lifecycle ownership mismatch');
END;

CREATE TRIGGER student_school_enrolments_lifecycle_ownership_update
BEFORE UPDATE OF student_id,source_case_id,application_cycle_id,lifecycle_context,
  confirmation_reason,confirmed_by,confirmed_at
ON student_school_enrolments
WHEN (NEW.source_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
   WHERE id=NEW.source_case_id AND student_id=NEW.student_id
)) OR (NEW.application_cycle_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM application_cycles
   WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
     AND (NEW.source_case_id IS NULL OR source_student_case_id=NEW.source_case_id)
     AND (NEW.lifecycle_context IS NULL OR cycle_type=NEW.lifecycle_context)
)) OR ((NEW.confirmed_by IS NULL OR NEW.confirmed_at IS NULL
         OR NEW.confirmation_reason IS NULL)
        AND NOT (NEW.confirmed_by IS NULL AND NEW.confirmed_at IS NULL
                 AND NEW.confirmation_reason IS NULL))
BEGIN
  SELECT RAISE(ABORT,'school enrolment lifecycle ownership mismatch');
END;

CREATE TRIGGER school_enrolment_transition_intents_ownership_insert
BEFORE INSERT ON school_enrolment_transition_intents
WHEN NOT EXISTS (
  SELECT 1 FROM student_school_enrolments AS current_row
  JOIN student_school_enrolments AS upcoming_row
    ON upcoming_row.id=NEW.upcoming_enrolment_id
  WHERE current_row.id=NEW.current_enrolment_id
    AND current_row.student_id=NEW.student_id
    AND current_row.enrolment_role='current' AND current_row.status='active'
    AND upcoming_row.student_id=NEW.student_id
    AND upcoming_row.enrolment_role='upcoming' AND upcoming_row.status='active'
    AND (NEW.source_case_id IS NULL OR upcoming_row.source_case_id=NEW.source_case_id)
) OR (NEW.source_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
   WHERE id=NEW.source_case_id AND student_id=NEW.student_id
)) OR (NEW.application_cycle_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM application_cycles
   WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
     AND (NEW.source_case_id IS NULL OR source_student_case_id=NEW.source_case_id)
))
BEGIN
  SELECT RAISE(ABORT,'school enrolment transition ownership mismatch');
END;

CREATE TRIGGER school_enrolment_transition_intents_no_delete
BEFORE DELETE ON school_enrolment_transition_intents
BEGIN
  SELECT RAISE(ABORT,'school enrolment transition intents are retained permanently');
END;

CREATE TRIGGER school_enrolment_events_ownership_insert
BEFORE INSERT ON school_enrolment_events
WHEN (NEW.enrolment_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_school_enrolments
   WHERE id=NEW.enrolment_id AND student_id=NEW.student_id
)) OR (NEW.transition_intent_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM school_enrolment_transition_intents
   WHERE id=NEW.transition_intent_id AND student_id=NEW.student_id
)) OR (NEW.source_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
   WHERE id=NEW.source_case_id AND student_id=NEW.student_id
)) OR (NEW.application_cycle_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM application_cycles
   WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
))
BEGIN
  SELECT RAISE(ABORT,'school enrolment event ownership mismatch');
END;

CREATE TRIGGER school_enrolment_events_no_update
BEFORE UPDATE ON school_enrolment_events
BEGIN
  SELECT RAISE(ABORT,'school enrolment events are append-only');
END;

CREATE TRIGGER school_enrolment_events_no_delete
BEFORE DELETE ON school_enrolment_events
BEGIN
  SELECT RAISE(ABORT,'school enrolment events are append-only');
END;
