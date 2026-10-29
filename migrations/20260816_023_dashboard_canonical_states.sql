-- migration_mode: sqlite_fk_off_rebuild
-- Canonical Dashboard linkage: HE offers, retained acceptance choices, offer
-- validity events, and visa business decision dates. Forward-only.

DROP TRIGGER offer_received_events_ownership_insert;
DROP TRIGGER offer_received_events_no_update;
DROP TRIGGER offer_received_events_no_delete;

ALTER TABLE offer_received_events RENAME TO offer_received_events_legacy_023;

CREATE TABLE offer_received_events (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL
    CHECK(source_kind IN ('public','private','higher_education')),
  public_application_group_id INTEGER
    REFERENCES public_application_groups(id) ON DELETE RESTRICT,
  private_application_id INTEGER
    REFERENCES private_school_applications(id) ON DELETE RESTRICT,
  higher_education_offer_id INTEGER
    REFERENCES higher_education_offers(id) ON DELETE RESTRICT,
  legacy_offer_id INTEGER REFERENCES offers(id) ON DELETE RESTRICT,
  school_id INTEGER,
  school_name_snapshot TEXT NOT NULL,
  external_offer_key TEXT NOT NULL CHECK(length(trim(external_offer_key)) > 0),
  idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
  received_at TEXT NOT NULL,
  recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  recorded_by_name TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (source_kind='public' AND public_application_group_id IS NOT NULL
      AND private_application_id IS NULL
      AND higher_education_offer_id IS NULL)
    OR
    (source_kind='private' AND private_application_id IS NOT NULL
      AND public_application_group_id IS NULL
      AND higher_education_offer_id IS NULL)
    OR
    (source_kind='higher_education' AND higher_education_offer_id IS NOT NULL
      AND public_application_group_id IS NULL
      AND private_application_id IS NULL)
  ),
  UNIQUE(application_cycle_id,source_kind,external_offer_key)
);

INSERT INTO offer_received_events(
  id,application_cycle_id,student_id,source_kind,
  public_application_group_id,private_application_id,higher_education_offer_id,
  legacy_offer_id,school_id,school_name_snapshot,external_offer_key,
  idempotency_key,received_at,recorded_by_user_id,recorded_by_name,created_at
)
SELECT id,application_cycle_id,student_id,source_kind,
       public_application_group_id,private_application_id,NULL,
       legacy_offer_id,school_id,school_name_snapshot,external_offer_key,
       idempotency_key,received_at,recorded_by_user_id,recorded_by_name,created_at
FROM offer_received_events_legacy_023;

DROP TABLE offer_received_events_legacy_023;

CREATE INDEX idx_offer_received_events_cycle_received
ON offer_received_events(application_cycle_id,received_at,id);

CREATE UNIQUE INDEX uq_offer_received_events_higher_education_offer
ON offer_received_events(higher_education_offer_id)
WHERE higher_education_offer_id IS NOT NULL;

CREATE TRIGGER offer_received_events_ownership_insert
BEFORE INSERT ON offer_received_events
WHEN NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
OR (
  NEW.source_kind='public' AND NOT EXISTS (
    SELECT 1 FROM public_application_groups
    WHERE id=NEW.public_application_group_id
      AND application_cycle_id=NEW.application_cycle_id
      AND student_id=NEW.student_id
  )
)
OR (
  NEW.source_kind='private' AND NOT EXISTS (
    SELECT 1 FROM private_school_applications
    WHERE id=NEW.private_application_id
      AND application_cycle_id=NEW.application_cycle_id
      AND student_id=NEW.student_id
  )
)
OR (
  NEW.source_kind='higher_education' AND NOT EXISTS (
    SELECT 1
    FROM higher_education_offers AS offer
    JOIN higher_education_applications AS application
      ON application.id=offer.application_id
    WHERE offer.id=NEW.higher_education_offer_id
      AND offer.application_cycle_id=NEW.application_cycle_id
      AND application.application_cycle_id=NEW.application_cycle_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Offer event ownership mismatch');
END;

CREATE TRIGGER offer_received_events_no_update
BEFORE UPDATE ON offer_received_events
BEGIN
  SELECT RAISE(ABORT, 'Offer events are append-only');
END;

CREATE TRIGGER offer_received_events_no_delete
BEFORE DELETE ON offer_received_events
BEGIN
  SELECT RAISE(ABORT, 'Offer events are append-only');
END;

CREATE TABLE offer_received_event_state_events (
  id INTEGER PRIMARY KEY,
  offer_received_event_id INTEGER NOT NULL
    REFERENCES offer_received_events(id) ON DELETE RESTRICT,
  state TEXT NOT NULL
    CHECK(state IN ('ACTIVE','VOID','WITHDRAWN','SUPERSEDED','INVALID_RECORD')),
  reason TEXT,
  occurred_at TEXT NOT NULL,
  actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  actor_name TEXT,
  idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
  created_at TEXT NOT NULL
);

CREATE INDEX idx_offer_received_event_state_latest
ON offer_received_event_state_events(offer_received_event_id,occurred_at DESC,id DESC);

CREATE TRIGGER offer_received_event_state_events_no_update
BEFORE UPDATE ON offer_received_event_state_events
BEGIN
  SELECT RAISE(ABORT, 'offer state events are append-only');
END;

CREATE TRIGGER offer_received_event_state_events_no_delete
BEFORE DELETE ON offer_received_event_state_events
BEGIN
  SELECT RAISE(ABORT, 'offer state events are append-only');
END;

DROP TRIGGER cycle_final_acceptances_ownership_insert;
DROP TRIGGER cycle_final_acceptances_no_delete;

ALTER TABLE cycle_final_acceptances RENAME TO cycle_final_acceptances_legacy_023;

CREATE TABLE cycle_final_acceptances (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  offer_received_event_id INTEGER NOT NULL UNIQUE
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
  updated_at TEXT NOT NULL,
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

INSERT INTO cycle_final_acceptances(
  id,application_cycle_id,student_id,offer_received_event_id,school_id,
  school_name_snapshot,signed_offer_completed_at,payment_completed_at,
  selected_at,selected_by_user_id,selected_by_name,acceptance_status,
  ended_at,ended_by_user_id,ended_by_name,status_reason,
  replaces_acceptance_id,superseded_by_acceptance_id,created_at,updated_at
)
SELECT id,application_cycle_id,student_id,offer_received_event_id,school_id,
       school_name_snapshot,signed_offer_completed_at,payment_completed_at,
       selected_at,selected_by_user_id,selected_by_name,'active',
       NULL,NULL,NULL,NULL,NULL,NULL,created_at,updated_at
FROM cycle_final_acceptances_legacy_023;

DROP TABLE cycle_final_acceptances_legacy_023;

CREATE UNIQUE INDEX uq_cycle_final_acceptances_one_active
ON cycle_final_acceptances(application_cycle_id)
WHERE acceptance_status='active';

CREATE INDEX idx_cycle_final_acceptances_cycle_status
ON cycle_final_acceptances(application_cycle_id,acceptance_status,selected_at,id);

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

DROP TRIGGER cycle_final_acceptance_history_no_update;
DROP TRIGGER cycle_final_acceptance_history_no_delete;

ALTER TABLE cycle_final_acceptance_history
RENAME TO cycle_final_acceptance_history_legacy_023;

CREATE TABLE cycle_final_acceptance_history (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  final_acceptance_id INTEGER NOT NULL
    REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL
    CHECK(event_type IN (
      'selected','signed_offer_completed','payment_completed','corrected',
      'withdrawn','superseded','replacement_selected'
    )),
  old_value_json TEXT,
  new_value_json TEXT,
  occurred_at TEXT NOT NULL,
  actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  actor_name TEXT,
  created_at TEXT NOT NULL
);

INSERT INTO cycle_final_acceptance_history(
  id,application_cycle_id,final_acceptance_id,event_type,old_value_json,
  new_value_json,occurred_at,actor_user_id,actor_name,created_at
)
SELECT id,application_cycle_id,final_acceptance_id,event_type,old_value_json,
       new_value_json,occurred_at,actor_user_id,actor_name,created_at
FROM cycle_final_acceptance_history_legacy_023;

DROP TABLE cycle_final_acceptance_history_legacy_023;

CREATE INDEX idx_final_acceptance_history_cycle
ON cycle_final_acceptance_history(application_cycle_id,occurred_at,id);

CREATE TRIGGER cycle_final_acceptance_history_no_update
BEFORE UPDATE ON cycle_final_acceptance_history
BEGIN
  SELECT RAISE(ABORT, 'final acceptance history is append-only');
END;

CREATE TRIGGER cycle_final_acceptance_history_no_delete
BEFORE DELETE ON cycle_final_acceptance_history
BEGIN
  SELECT RAISE(ABORT, 'final acceptance history is append-only');
END;

DROP TRIGGER final_school_submission_ownership_insert;

CREATE TRIGGER final_school_submission_ownership_insert
BEFORE INSERT ON final_school_submission_events
WHEN NOT EXISTS (
  SELECT 1 FROM cycle_final_acceptances
  WHERE id=NEW.final_acceptance_id
    AND application_cycle_id=NEW.application_cycle_id
    AND acceptance_status='active'
    AND signed_offer_completed_at IS NOT NULL
    AND payment_completed_at IS NOT NULL
)
BEGIN
  SELECT RAISE(ABORT, 'final school submission ownership or evidence mismatch');
END;

DROP TRIGGER visa_decision_events_ownership_insert;
DROP TRIGGER visa_decision_events_no_update;
DROP TRIGGER visa_decision_events_no_delete;

ALTER TABLE visa_decision_events RENAME TO visa_decision_events_legacy_023;

CREATE TABLE visa_decision_events (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL UNIQUE
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE RESTRICT,
  student_document_id INTEGER UNIQUE
    REFERENCES student_documents(id) ON DELETE RESTRICT,
  visa_record_id INTEGER UNIQUE REFERENCES visas(id) ON DELETE RESTRICT,
  decision_type TEXT NOT NULL CHECK(decision_type IN ('grant','refusal')),
  decision_date TEXT,
  confirmed_at TEXT NOT NULL,
  evidence_uploaded_at TEXT,
  decision_source TEXT NOT NULL DEFAULT 'visa_record'
    CHECK(decision_source IN ('visa_record','legacy_document')),
  idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
  recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  recorded_by_name TEXT,
  created_at TEXT NOT NULL,
  CHECK(
    (decision_source='visa_record' AND visa_record_id IS NOT NULL
      AND decision_date IS NOT NULL AND length(trim(decision_date)) > 0)
    OR
    (decision_source='legacy_document' AND student_document_id IS NOT NULL)
  )
);

INSERT INTO visa_decision_events(
  id,application_cycle_id,student_id,visa_case_id,student_document_id,
  visa_record_id,decision_type,decision_date,confirmed_at,evidence_uploaded_at,
  decision_source,idempotency_key,recorded_by_user_id,recorded_by_name,created_at
)
SELECT id,application_cycle_id,student_id,visa_case_id,student_document_id,
       NULL,decision_type,NULL,confirmed_at,confirmed_at,
       'legacy_document',idempotency_key,recorded_by_user_id,recorded_by_name,created_at
FROM visa_decision_events_legacy_023;

DROP TABLE visa_decision_events_legacy_023;

CREATE INDEX idx_visa_decision_events_type_date
ON visa_decision_events(decision_type,decision_date,application_cycle_id);

CREATE TRIGGER visa_decision_events_ownership_insert
BEFORE INSERT ON visa_decision_events
WHEN NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
OR NOT EXISTS (
  SELECT 1 FROM visa_cases
  WHERE id=NEW.visa_case_id
    AND student_id=NEW.student_id
    AND application_cycle_id=NEW.application_cycle_id
)
OR (
  NEW.student_document_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM student_documents
    WHERE id=NEW.student_document_id
      AND student_id=NEW.student_id
      AND is_active=1
      AND (
        (NEW.decision_type='grant' AND document_type='VisaGrantLetter')
        OR
        (NEW.decision_type='refusal' AND document_type='VisaRefusalLetter')
      )
  )
)
OR (
  NEW.visa_record_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM visas
    WHERE id=NEW.visa_record_id
      AND student_id=NEW.student_id
      AND (
        (NEW.decision_type='grant' AND lower(trim(visa_status)) IN ('granted','active'))
        OR
        (NEW.decision_type='refusal' AND lower(trim(visa_status))='refused')
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'visa decision ownership or evidence mismatch');
END;

CREATE TRIGGER visa_decision_events_no_update
BEFORE UPDATE ON visa_decision_events
BEGIN
  SELECT RAISE(ABORT, 'visa decision events are append-only');
END;

CREATE TRIGGER visa_decision_events_no_delete
BEFORE DELETE ON visa_decision_events
BEGIN
  SELECT RAISE(ABORT, 'visa decision events are append-only');
END;
