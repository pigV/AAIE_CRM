ALTER TABLE school_plan_reports
    ADD COLUMN city_id INTEGER REFERENCES school_cities(id);

ALTER TABLE school_plan_reports
    ADD COLUMN city_name_snapshot TEXT;

ALTER TABLE school_plan_reports
    ADD COLUMN plan_sector TEXT CHECK(plan_sector IN ('public','private'));

ALTER TABLE school_plan_reports
    ADD COLUMN is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1));

ALTER TABLE school_plan_reports
    ADD COLUMN superseded_by_report_id INTEGER REFERENCES school_plan_reports(id);

ALTER TABLE school_plan_reports
    ADD COLUMN scope_review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(scope_review_status IN ('pending','resolved','needs_review'));

ALTER TABLE offers
    ADD COLUMN school_id INTEGER REFERENCES schools(id);

ALTER TABLE offers
    ADD COLUMN city_id INTEGER REFERENCES school_cities(id);

ALTER TABLE offers
    ADD COLUMN city_name_snapshot TEXT;

ALTER TABLE offers
    ADD COLUMN sector_key TEXT CHECK(sector_key IN ('public','private'));

ALTER TABLE offers
    ADD COLUMN classification_status TEXT NOT NULL DEFAULT 'unresolved'
        CHECK(classification_status IN ('resolved','unresolved','ambiguous'));

CREATE TABLE school_plan_confirmed_schools (
  id INTEGER PRIMARY KEY,
  confirmation_id INTEGER NOT NULL REFERENCES school_plan_report_confirmations(id) ON DELETE CASCADE,
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  school_id INTEGER REFERENCES schools(id),
  school_name_snapshot TEXT NOT NULL,
  city_id INTEGER REFERENCES school_cities(id),
  city_name_snapshot TEXT,
  plan_sector TEXT NOT NULL CHECK(plan_sector IN ('public','private')),
  decision_mode TEXT NOT NULL CHECK(decision_mode IN ('ranked','selected')),
  rank_order INTEGER,
  is_selected INTEGER NOT NULL DEFAULT 1 CHECK(is_selected IN (0,1)),
  submission_status TEXT NOT NULL DEFAULT 'not_submitted' CHECK(submission_status IN ('not_submitted','submitted')),
  submitted_at TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id),
  submitted_by_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(confirmation_id, school_id, school_name_snapshot)
);

CREATE TRIGGER school_plan_confirmed_schools_ownership_insert
BEFORE INSERT ON school_plan_confirmed_schools
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_report_confirmations
  WHERE id=NEW.confirmation_id AND report_id=NEW.report_id
)
OR NOT EXISTS (
  SELECT 1 FROM school_plan_reports
  WHERE id=NEW.report_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed school ownership mismatch');
END;

CREATE TRIGGER school_plan_confirmed_schools_ownership_update
BEFORE UPDATE OF confirmation_id,report_id,student_id ON school_plan_confirmed_schools
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_report_confirmations
  WHERE id=NEW.confirmation_id AND report_id=NEW.report_id
)
OR NOT EXISTS (
  SELECT 1 FROM school_plan_reports
  WHERE id=NEW.report_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed school ownership mismatch');
END;

CREATE UNIQUE INDEX uq_school_plan_confirmed_schools_null_school
ON school_plan_confirmed_schools(confirmation_id, school_name_snapshot)
WHERE school_id IS NULL;

CREATE TRIGGER school_plan_confirmed_schools_not_submitted_evidence_insert
BEFORE INSERT ON school_plan_confirmed_schools
WHEN NEW.submission_status='not_submitted'
 AND (NEW.submitted_at IS NOT NULL
      OR NEW.submitted_by_user_id IS NOT NULL
      OR NEW.submitted_by_name IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'not submitted schools cannot retain submission evidence');
END;

CREATE TRIGGER school_plan_confirmed_schools_not_submitted_evidence_update
BEFORE UPDATE OF submission_status,submitted_at,submitted_by_user_id,submitted_by_name
ON school_plan_confirmed_schools
WHEN NEW.submission_status='not_submitted'
 AND (NEW.submitted_at IS NOT NULL
      OR NEW.submitted_by_user_id IS NOT NULL
      OR NEW.submitted_by_name IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'not submitted schools cannot retain submission evidence');
END;

-- Service-internal audit table: application services must never insert rows directly.
-- Submission-status transition triggers are the sole supported event writer.
CREATE TABLE school_plan_school_submission_events (
  id INTEGER PRIMARY KEY,
  confirmed_school_id INTEGER NOT NULL REFERENCES school_plan_confirmed_schools(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE RESTRICT,
  previous_submission_status TEXT NOT NULL CHECK(previous_submission_status IN ('not_submitted','submitted')),
  new_submission_status TEXT NOT NULL CHECK(new_submission_status IN ('not_submitted','submitted')),
  submitted_at TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id),
  submitted_by_name TEXT,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TRIGGER school_plan_school_submission_events_ownership_insert
BEFORE INSERT ON school_plan_school_submission_events
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools
  WHERE id=NEW.confirmed_school_id
    AND report_id=NEW.report_id
    AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'submission event ownership mismatch');
END;

CREATE TRIGGER school_plan_school_submission_events_ownership_update
BEFORE UPDATE OF confirmed_school_id,student_id,report_id ON school_plan_school_submission_events
WHEN NOT EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools
  WHERE id=NEW.confirmed_school_id
    AND report_id=NEW.report_id
    AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'submission event ownership mismatch');
END;

CREATE TRIGGER school_plan_confirmed_schools_identity_no_update_after_event
BEFORE UPDATE OF confirmation_id,report_id,student_id ON school_plan_confirmed_schools
WHEN EXISTS (
  SELECT 1 FROM school_plan_school_submission_events
  WHERE confirmed_school_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'submitted school identity is immutable');
END;

CREATE TRIGGER school_plan_confirmed_schools_submission_event_after_update
AFTER UPDATE OF submission_status ON school_plan_confirmed_schools
WHEN OLD.submission_status <> NEW.submission_status
BEGIN
  INSERT INTO school_plan_school_submission_events(
    confirmed_school_id,student_id,report_id,
    previous_submission_status,new_submission_status,
    submitted_at,submitted_by_user_id,submitted_by_name,
    occurred_at,created_at
  ) VALUES(
    NEW.id,NEW.student_id,NEW.report_id,
    OLD.submission_status,NEW.submission_status,
    CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_at END,
    CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_by_user_id END,
    CASE WHEN NEW.submission_status='submitted' THEN NEW.submitted_by_name END,
    NEW.updated_at,NEW.updated_at
  );
END;

CREATE TRIGGER school_plan_school_submission_events_no_update
BEFORE UPDATE ON school_plan_school_submission_events
BEGIN
  SELECT RAISE(ABORT, 'school plan submission events are append-only');
END;

CREATE TRIGGER school_plan_school_submission_events_no_delete
BEFORE DELETE ON school_plan_school_submission_events
BEGIN
  SELECT RAISE(ABORT, 'school plan submission events are append-only');
END;

CREATE TABLE student_offer_acceptances (
  student_id TEXT PRIMARY KEY REFERENCES students(student_id) ON DELETE CASCADE,
  offer_id INTEGER NOT NULL UNIQUE REFERENCES offers(id) ON DELETE CASCADE,
  status_before_acceptance TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  accepted_by_user_id INTEGER REFERENCES users(id),
  accepted_by_name TEXT,
  updated_at TEXT NOT NULL
);

CREATE TRIGGER student_offer_acceptances_ownership_insert
BEFORE INSERT ON student_offer_acceptances
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.offer_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'accepted Offer ownership mismatch');
END;

CREATE TRIGGER student_offer_acceptances_ownership_update
BEFORE UPDATE OF student_id,offer_id ON student_offer_acceptances
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.offer_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'accepted Offer ownership mismatch');
END;

CREATE TABLE offer_acceptance_history (
  id INTEGER PRIMARY KEY,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  previous_offer_id INTEGER REFERENCES offers(id) ON DELETE RESTRICT,
  new_offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE RESTRICT,
  previous_offer_restored_status TEXT,
  new_offer_previous_status TEXT NOT NULL,
  accepted_by_user_id INTEGER REFERENCES users(id),
  accepted_by_name TEXT,
  accepted_at TEXT NOT NULL,
  coe_present INTEGER NOT NULL CHECK(coe_present IN (0,1)),
  coe_warning_acknowledged INTEGER NOT NULL CHECK(coe_warning_acknowledged IN (0,1)),
  created_at TEXT NOT NULL
);

CREATE TRIGGER offer_acceptance_history_ownership_insert
BEFORE INSERT ON offer_acceptance_history
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.new_offer_id AND student_id=NEW.student_id
)
OR (
  NEW.previous_offer_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM offers
    WHERE id=NEW.previous_offer_id AND student_id=NEW.student_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Offer acceptance history ownership mismatch');
END;

CREATE TRIGGER offer_acceptance_history_ownership_update
BEFORE UPDATE OF student_id,previous_offer_id,new_offer_id ON offer_acceptance_history
WHEN NOT EXISTS (
  SELECT 1 FROM offers
  WHERE id=NEW.new_offer_id AND student_id=NEW.student_id
)
OR (
  NEW.previous_offer_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM offers
    WHERE id=NEW.previous_offer_id AND student_id=NEW.student_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Offer acceptance history ownership mismatch');
END;

CREATE TRIGGER offer_acceptance_history_no_update
BEFORE UPDATE ON offer_acceptance_history
BEGIN
  SELECT RAISE(ABORT, 'offer acceptance history is append-only');
END;

CREATE TRIGGER offer_acceptance_history_no_delete
BEFORE DELETE ON offer_acceptance_history
BEGIN
  SELECT RAISE(ABORT, 'offer acceptance history is append-only');
END;

CREATE TRIGGER offers_student_identity_no_update
BEFORE UPDATE OF student_id ON offers
WHEN EXISTS (
  SELECT 1 FROM student_offer_acceptances
  WHERE offer_id=OLD.id
)
OR EXISTS (
  SELECT 1 FROM offer_acceptance_history
  WHERE previous_offer_id=OLD.id OR new_offer_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'referenced Offer ownership is immutable');
END;

CREATE TRIGGER school_plan_reports_student_identity_no_update
BEFORE UPDATE OF student_id ON school_plan_reports
WHEN EXISTS (
  SELECT 1 FROM school_plan_report_confirmations WHERE report_id=OLD.id
)
OR EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools WHERE report_id=OLD.id
)
OR EXISTS (
  SELECT 1 FROM school_plan_school_submission_events WHERE report_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'referenced school plan report ownership is immutable');
END;

CREATE TRIGGER school_plan_report_confirmations_report_identity_no_update
BEFORE UPDATE OF report_id ON school_plan_report_confirmations
WHEN EXISTS (
  SELECT 1 FROM school_plan_confirmed_schools
  WHERE confirmation_id=OLD.id
)
BEGIN
  SELECT RAISE(ABORT, 'confirmed school report identity is immutable');
END;

CREATE TABLE school_plan_offer_migration_exceptions (
  id INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  student_id TEXT REFERENCES students(student_id),
  exception_type TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by_user_id INTEGER REFERENCES users(id),
  resolution_notes TEXT
);

CREATE UNIQUE INDEX uq_school_plan_current_public_city
ON school_plan_reports(student_id, city_id)
WHERE plan_sector='public' AND is_current=1;
