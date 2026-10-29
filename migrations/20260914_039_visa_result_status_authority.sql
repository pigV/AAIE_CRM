CREATE TABLE visa_result_status_events(
  id INTEGER PRIMARY KEY,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
  document_id INTEGER REFERENCES visa_case_documents(id),
  event_kind TEXT NOT NULL CHECK(event_kind IN ('auto','manual')),
  detected_result TEXT CHECK(detected_result IN ('Granted','Refused')),
  previous_effective_result TEXT NOT NULL CHECK(previous_effective_result IN ('Pending','Granted','Refused','Withdrawn')),
  effective_result TEXT NOT NULL CHECK(effective_result IN ('Pending','Granted','Refused','Withdrawn')),
  conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0,1)),
  evidence_json TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  actor_user_id INTEGER NOT NULL REFERENCES users(id),
  occurred_at TEXT NOT NULL,
  CHECK(
    (event_kind='auto' AND document_id IS NOT NULL AND detected_result IS NOT NULL)
    OR
    (event_kind='manual' AND detected_result IS NULL)
  )
);

CREATE TABLE visa_result_status_current(
  visa_case_id INTEGER PRIMARY KEY REFERENCES visa_cases(id),
  effective_result TEXT NOT NULL CHECK(effective_result IN ('Pending','Granted','Refused','Withdrawn')),
  authority TEXT NOT NULL CHECK(authority IN ('legacy','auto','manual')),
  auto_detected_result TEXT CHECK(auto_detected_result IN ('Granted','Refused')),
  manual_override INTEGER NOT NULL DEFAULT 0 CHECK(manual_override IN (0,1)),
  conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0,1)),
  source_event_id INTEGER REFERENCES visa_result_status_events(id),
  updated_by INTEGER NOT NULL REFERENCES users(id),
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_visa_result_status_events_case
ON visa_result_status_events(visa_case_id,id);

CREATE INDEX idx_visa_result_status_events_document
ON visa_result_status_events(document_id,id);

CREATE TRIGGER visa_result_status_events_no_update
BEFORE UPDATE ON visa_result_status_events
BEGIN
  SELECT RAISE(ABORT,'Visa result status evidence immutable');
END;
CREATE TRIGGER visa_result_status_events_no_delete
BEFORE DELETE ON visa_result_status_events
BEGIN
  SELECT RAISE(ABORT,'Visa result status evidence immutable');
END;

CREATE TRIGGER visa_result_status_events_document_authority
BEFORE INSERT ON visa_result_status_events
WHEN NEW.document_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1
    FROM visa_case_documents d
    WHERE d.id=NEW.document_id
      AND d.visa_case_id=NEW.visa_case_id
      AND d.archived_at IS NULL
  ) THEN RAISE(ABORT,'Visa result document authority invalid') END;
END;
