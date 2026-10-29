ALTER TABLE school_plan_confirmed_schools
    ADD COLUMN result_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(result_status IN ('pending','offer_received','rejected','no_place_or_closed','withdrawn','voided'));
ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_at TEXT;
ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_by_user_id INTEGER REFERENCES users(id);
ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_by_name TEXT;
ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_note TEXT;
ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_offer_id INTEGER REFERENCES offers(id);

ALTER TABLE visa_cases ADD COLUMN lodged_by_user_id INTEGER REFERENCES users(id);
ALTER TABLE visa_cases ADD COLUMN lodged_by_name TEXT;
ALTER TABLE visa_cases ADD COLUMN lodgement_recorded_at TEXT;

CREATE TABLE school_plan_school_result_events (
  id INTEGER PRIMARY KEY,
  confirmed_school_id INTEGER NOT NULL REFERENCES school_plan_confirmed_schools(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id),
  report_id INTEGER NOT NULL REFERENCES school_plan_reports(id) ON DELETE RESTRICT,
  previous_result_status TEXT NOT NULL,
  new_result_status TEXT NOT NULL,
  result_at TEXT,
  result_by_user_id INTEGER REFERENCES users(id),
  result_by_name TEXT,
  result_note TEXT,
  result_offer_id INTEGER REFERENCES offers(id),
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_school_plan_confirmed_schools_result
ON school_plan_confirmed_schools(student_id,result_status);
CREATE INDEX idx_school_plan_result_events_school
ON school_plan_school_result_events(confirmed_school_id,id);

CREATE TRIGGER school_plan_school_result_evidence_insert
BEFORE INSERT ON school_plan_confirmed_schools
WHEN (NEW.result_status='pending' AND (
        NEW.result_at IS NOT NULL OR NEW.result_by_user_id IS NOT NULL OR
        NEW.result_by_name IS NOT NULL OR NEW.result_note IS NOT NULL OR
        NEW.result_offer_id IS NOT NULL
      ))
  OR (NEW.result_status<>'pending' AND (
        NEW.submission_status<>'submitted' OR NEW.result_at IS NULL OR
        NEW.result_by_user_id IS NULL OR trim(coalesce(NEW.result_by_name,''))=''
      ))
  OR (NEW.result_status='offer_received' AND NEW.result_offer_id IS NULL)
  OR (NEW.result_status<>'offer_received' AND NEW.result_offer_id IS NOT NULL)
  OR (NEW.result_status IN ('rejected','no_place_or_closed','withdrawn','voided')
      AND trim(coalesce(NEW.result_note,''))='')
BEGIN
  SELECT RAISE(ABORT, 'invalid school result evidence');
END;

CREATE TRIGGER school_plan_school_result_evidence_update
BEFORE UPDATE OF result_status,result_at,result_by_user_id,result_by_name,result_note,result_offer_id
ON school_plan_confirmed_schools
WHEN (NEW.result_status='pending' AND (
        NEW.result_at IS NOT NULL OR NEW.result_by_user_id IS NOT NULL OR
        NEW.result_by_name IS NOT NULL OR NEW.result_note IS NOT NULL OR
        NEW.result_offer_id IS NOT NULL
      ))
  OR (NEW.result_status<>'pending' AND (
        NEW.submission_status<>'submitted' OR NEW.result_at IS NULL OR
        NEW.result_by_user_id IS NULL OR trim(coalesce(NEW.result_by_name,''))=''
      ))
  OR (NEW.result_status='offer_received' AND NEW.result_offer_id IS NULL)
  OR (NEW.result_status<>'offer_received' AND NEW.result_offer_id IS NOT NULL)
  OR (NEW.result_status IN ('rejected','no_place_or_closed','withdrawn','voided')
      AND trim(coalesce(NEW.result_note,''))='')
BEGIN
  SELECT RAISE(ABORT, 'invalid school result evidence');
END;

CREATE TRIGGER school_plan_school_result_offer_ownership_insert
BEFORE INSERT ON school_plan_confirmed_schools
WHEN NEW.result_offer_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM offers WHERE id=NEW.result_offer_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'school result offer ownership mismatch');
END;

CREATE TRIGGER school_plan_school_result_offer_ownership_update
BEFORE UPDATE OF result_offer_id,student_id ON school_plan_confirmed_schools
WHEN NEW.result_offer_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM offers WHERE id=NEW.result_offer_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'school result offer ownership mismatch');
END;

CREATE TRIGGER school_plan_school_result_event_after_update
AFTER UPDATE OF result_status ON school_plan_confirmed_schools
WHEN OLD.result_status <> NEW.result_status
BEGIN
  INSERT INTO school_plan_school_result_events(
    confirmed_school_id,student_id,report_id,previous_result_status,new_result_status,
    result_at,result_by_user_id,result_by_name,result_note,result_offer_id,
    occurred_at,created_at
  ) VALUES(
    NEW.id,NEW.student_id,NEW.report_id,OLD.result_status,NEW.result_status,
    NEW.result_at,NEW.result_by_user_id,NEW.result_by_name,NEW.result_note,NEW.result_offer_id,
    NEW.updated_at,NEW.updated_at
  );
END;

CREATE TRIGGER school_plan_school_result_events_no_update
BEFORE UPDATE ON school_plan_school_result_events
BEGIN
  SELECT RAISE(ABORT, 'school plan result events are append-only');
END;

CREATE TRIGGER school_plan_school_result_events_no_delete
BEFORE DELETE ON school_plan_school_result_events
BEGIN
  SELECT RAISE(ABORT, 'school plan result events are append-only');
END;
