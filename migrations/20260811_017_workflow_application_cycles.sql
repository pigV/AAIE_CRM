CREATE TABLE application_cycles (
  id INTEGER PRIMARY KEY,
  cycle_uid TEXT NOT NULL UNIQUE CHECK(length(trim(cycle_uid)) > 0),
  student_id TEXT REFERENCES students(student_id) ON DELETE RESTRICT,
  parent_form_token TEXT REFERENCES parent_form_tokens(token) ON DELETE RESTRICT,
  source_student_case_id INTEGER REFERENCES student_cases(id) ON DELETE RESTRICT,
  cycle_type TEXT NOT NULL
    CHECK(cycle_type IN ('initial','transfer','progression','future')),
  sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
  status TEXT NOT NULL CHECK(status IN ('active','closed')),
  outcome TEXT
    CHECK(outcome IS NULL OR outcome IN (
      'granted','refused','completed','withdrawn','cancelled'
    )),
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_by_name TEXT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_by_name TEXT,
  updated_at TEXT NOT NULL,
  CHECK(
    (status='active' AND outcome IS NULL AND closed_at IS NULL)
    OR
    (status='closed' AND outcome IS NOT NULL AND closed_at IS NOT NULL)
  ),
  CHECK(student_id IS NOT NULL OR parent_form_token IS NOT NULL),
  CHECK(
    (cycle_type='initial' AND source_student_case_id IS NULL)
    OR
    (cycle_type IN ('transfer','progression') AND source_student_case_id IS NOT NULL)
    OR
    cycle_type='future'
  )
);

CREATE UNIQUE INDEX uq_application_cycles_active_student
ON application_cycles(student_id)
WHERE status='active' AND student_id IS NOT NULL;

CREATE UNIQUE INDEX uq_application_cycles_student_sequence
ON application_cycles(student_id,sequence_no)
WHERE student_id IS NOT NULL;

CREATE UNIQUE INDEX uq_application_cycles_parent_form
ON application_cycles(parent_form_token)
WHERE parent_form_token IS NOT NULL;

CREATE UNIQUE INDEX uq_application_cycles_student_case
ON application_cycles(source_student_case_id)
WHERE source_student_case_id IS NOT NULL;

CREATE INDEX idx_application_cycles_status
ON application_cycles(status,cycle_type,id);

CREATE TRIGGER application_cycles_student_attach_once
BEFORE UPDATE OF student_id ON application_cycles
WHEN OLD.student_id IS NOT NULL AND NEW.student_id IS NOT OLD.student_id
BEGIN
  SELECT RAISE(ABORT, 'application cycle student identity is immutable');
END;

CREATE TRIGGER application_cycles_case_ownership_insert
BEFORE INSERT ON application_cycles
WHEN NEW.source_student_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
  WHERE id=NEW.source_student_case_id
    AND student_id=NEW.student_id
    AND case_type=NEW.cycle_type
)
BEGIN
  SELECT RAISE(ABORT, 'application cycle case ownership mismatch');
END;

CREATE TRIGGER application_cycles_case_ownership_update
BEFORE UPDATE OF source_student_case_id,student_id,cycle_type ON application_cycles
WHEN NEW.source_student_case_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM student_cases
  WHERE id=NEW.source_student_case_id
    AND student_id=NEW.student_id
    AND case_type=NEW.cycle_type
)
BEGIN
  SELECT RAISE(ABORT, 'application cycle case ownership mismatch');
END;

CREATE TRIGGER application_cycles_no_delete
BEFORE DELETE ON application_cycles
BEGIN
  SELECT RAISE(ABORT, 'application cycles are retained permanently');
END;

ALTER TABLE contracts
ADD COLUMN application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT;

ALTER TABLE school_plan_reports
ADD COLUMN application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT;

ALTER TABLE school_plan_reports
ADD COLUMN cycle_is_current INTEGER NOT NULL DEFAULT 0 CHECK(cycle_is_current IN (0,1));

ALTER TABLE offers
ADD COLUMN application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT;

ALTER TABLE coe_oshc
ADD COLUMN application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT;

ALTER TABLE visa_cases
ADD COLUMN application_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX uq_school_plan_cycle_current_public_city
ON school_plan_reports(application_cycle_id,city_id)
WHERE application_cycle_id IS NOT NULL
  AND plan_sector='public'
  AND cycle_is_current=1;

CREATE INDEX idx_school_plan_reports_cycle
ON school_plan_reports(application_cycle_id,plan_sector,city_id,cycle_is_current);

CREATE INDEX idx_contracts_cycle
ON contracts(application_cycle_id);

CREATE INDEX idx_offers_cycle
ON offers(application_cycle_id);

CREATE INDEX idx_coe_oshc_cycle
ON coe_oshc(application_cycle_id);

CREATE INDEX idx_visa_cases_cycle
ON visa_cases(application_cycle_id);

CREATE TRIGGER school_plan_reports_cycle_ownership_insert
BEFORE INSERT ON school_plan_reports
WHEN NEW.application_cycle_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'school plan cycle ownership mismatch');
END;

CREATE TRIGGER school_plan_reports_cycle_ownership_update
BEFORE UPDATE OF application_cycle_id,student_id ON school_plan_reports
WHEN NEW.application_cycle_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'school plan cycle ownership mismatch');
END;

CREATE TABLE public_application_groups (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  city_id INTEGER REFERENCES school_cities(id) ON DELETE RESTRICT,
  report_id INTEGER REFERENCES school_plan_reports(id) ON DELETE RESTRICT,
  confirmation_id INTEGER
    REFERENCES school_plan_report_confirmations(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','submitted','waiting_response','closed','cancelled')),
  submitted_at TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  submitted_by_name TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(application_cycle_id,report_id),
  CHECK(
    (status='draft' AND submitted_at IS NULL)
    OR
    (status<>'draft' AND submitted_at IS NOT NULL)
  )
);

CREATE INDEX idx_public_application_groups_cycle_status
ON public_application_groups(application_cycle_id,status,city_id);

CREATE TRIGGER public_application_groups_ownership_insert
BEFORE INSERT ON public_application_groups
WHEN NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
OR (
  NEW.report_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM school_plan_reports
    WHERE id=NEW.report_id
      AND application_cycle_id=NEW.application_cycle_id
      AND student_id=NEW.student_id
      AND plan_sector='public'
  )
)
BEGIN
  SELECT RAISE(ABORT, 'public application group ownership mismatch');
END;

CREATE TRIGGER public_application_groups_no_delete
BEFORE DELETE ON public_application_groups
BEGIN
  SELECT RAISE(ABORT, 'public application groups are retained permanently');
END;

CREATE TABLE public_application_preferences (
  id INTEGER PRIMARY KEY,
  public_application_group_id INTEGER NOT NULL
    REFERENCES public_application_groups(id) ON DELETE RESTRICT,
  confirmed_school_id INTEGER
    REFERENCES school_plan_confirmed_schools(id) ON DELETE RESTRICT,
  school_id INTEGER,
  school_name_snapshot TEXT NOT NULL,
  preference_order INTEGER CHECK(preference_order IS NULL OR preference_order > 0),
  application_status TEXT NOT NULL DEFAULT 'planned'
    CHECK(application_status IN (
      'planned','submitted','interview','test','waiting_list',
      'additional_documents','offer_received','rejected','withdrawn','closed'
    )),
  result_at TEXT,
  result_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(public_application_group_id,confirmed_school_id)
);

CREATE INDEX idx_public_preferences_group_status
ON public_application_preferences(public_application_group_id,application_status);

CREATE TRIGGER public_application_preferences_no_delete
BEFORE DELETE ON public_application_preferences
BEGIN
  SELECT RAISE(ABORT, 'public application preferences are retained permanently');
END;

CREATE TABLE private_school_applications (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  confirmed_school_id INTEGER
    REFERENCES school_plan_confirmed_schools(id) ON DELETE RESTRICT,
  school_id INTEGER,
  school_name_snapshot TEXT NOT NULL,
  city_id INTEGER REFERENCES school_cities(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'not_submitted'
    CHECK(status IN (
      'not_submitted','submitted','interview','test','waiting_list',
      'additional_documents','offer_received','rejected','withdrawn','closed'
    )),
  submitted_at TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  submitted_by_name TEXT,
  result_at TEXT,
  result_note TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(application_cycle_id,confirmed_school_id),
  CHECK(
    (status='not_submitted' AND submitted_at IS NULL)
    OR
    (status<>'not_submitted' AND submitted_at IS NOT NULL)
  )
);

CREATE INDEX idx_private_applications_cycle_status
ON private_school_applications(application_cycle_id,status,city_id);

CREATE TRIGGER private_school_applications_ownership_insert
BEFORE INSERT ON private_school_applications
WHEN NOT EXISTS (
  SELECT 1 FROM application_cycles
  WHERE id=NEW.application_cycle_id AND student_id=NEW.student_id
)
BEGIN
  SELECT RAISE(ABORT, 'private application ownership mismatch');
END;

CREATE TRIGGER private_school_applications_no_delete
BEFORE DELETE ON private_school_applications
BEGIN
  SELECT RAISE(ABORT, 'private applications are retained permanently');
END;

CREATE TABLE offer_received_events (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('public','private')),
  public_application_group_id INTEGER
    REFERENCES public_application_groups(id) ON DELETE RESTRICT,
  private_application_id INTEGER
    REFERENCES private_school_applications(id) ON DELETE RESTRICT,
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
      AND private_application_id IS NULL)
    OR
    (source_kind='private' AND private_application_id IS NOT NULL
      AND public_application_group_id IS NULL)
  ),
  UNIQUE(application_cycle_id,source_kind,external_offer_key)
);

CREATE INDEX idx_offer_received_events_cycle_received
ON offer_received_events(application_cycle_id,received_at,id);

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

CREATE TABLE cycle_final_acceptances (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL UNIQUE
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
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

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

CREATE TRIGGER cycle_final_acceptances_no_delete
BEFORE DELETE ON cycle_final_acceptances
BEGIN
  SELECT RAISE(ABORT, 'final acceptances are retained permanently');
END;

CREATE TABLE cycle_final_acceptance_history (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  final_acceptance_id INTEGER NOT NULL
    REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL
    CHECK(event_type IN ('selected','signed_offer_completed','payment_completed','corrected')),
  old_value_json TEXT,
  new_value_json TEXT,
  occurred_at TEXT NOT NULL,
  actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  actor_name TEXT,
  created_at TEXT NOT NULL
);

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

CREATE TABLE final_school_submission_events (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  final_acceptance_id INTEGER NOT NULL UNIQUE
    REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  submitted_at TEXT NOT NULL,
  submission_channel TEXT NOT NULL CHECK(length(trim(submission_channel)) > 0),
  submission_reference TEXT,
  notes TEXT,
  idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
  submitted_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  submitted_by_name TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_final_school_submission_cycle
ON final_school_submission_events(application_cycle_id,submitted_at,id);

CREATE TRIGGER final_school_submission_ownership_insert
BEFORE INSERT ON final_school_submission_events
WHEN NOT EXISTS (
  SELECT 1 FROM cycle_final_acceptances
  WHERE id=NEW.final_acceptance_id
    AND application_cycle_id=NEW.application_cycle_id
    AND signed_offer_completed_at IS NOT NULL
    AND payment_completed_at IS NOT NULL
)
BEGIN
  SELECT RAISE(ABORT, 'final school submission ownership or evidence mismatch');
END;

CREATE TRIGGER final_school_submission_events_no_update
BEFORE UPDATE ON final_school_submission_events
BEGIN
  SELECT RAISE(ABORT, 'final school submission events are append-only');
END;

CREATE TRIGGER final_school_submission_events_no_delete
BEFORE DELETE ON final_school_submission_events
BEGIN
  SELECT RAISE(ABORT, 'final school submission events are append-only');
END;

CREATE TABLE visa_decision_events (
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER NOT NULL UNIQUE
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id) ON DELETE RESTRICT,
  student_document_id INTEGER NOT NULL UNIQUE
    REFERENCES student_documents(id) ON DELETE RESTRICT,
  decision_type TEXT NOT NULL CHECK(decision_type IN ('grant','refusal')),
  confirmed_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
  recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  recorded_by_name TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_visa_decision_events_type_confirmed
ON visa_decision_events(decision_type,confirmed_at,application_cycle_id);

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
OR NOT EXISTS (
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
BEGIN
  SELECT RAISE(ABORT, 'visa decision ownership or document mismatch');
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
