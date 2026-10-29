-- Transfer V2 is additive. Existing Transfer rows remain NULL/legacy and no
-- business facts are inserted or rewritten by this migration.

ALTER TABLE transfer_cases ADD COLUMN v2_version INTEGER
  CHECK(v2_version IS NULL OR v2_version=2);
ALTER TABLE transfer_cases ADD COLUMN transfer_source_label TEXT
  CHECK(transfer_source_label IS NULL OR transfer_source_label IN (
    'aaie_existing','new_transfer'
  ));
ALTER TABLE transfer_cases ADD COLUMN current_year_level_snapshot TEXT
  CHECK(current_year_level_snapshot IS NULL OR length(trim(current_year_level_snapshot))>0);
ALTER TABLE transfer_cases ADD COLUMN application_year_level TEXT
  CHECK(application_year_level IS NULL OR length(trim(application_year_level))>0);
ALTER TABLE transfer_cases ADD COLUMN transfer_year INTEGER
  CHECK(transfer_year IS NULL OR transfer_year BETWEEN 2000 AND 2200);
ALTER TABLE transfer_cases ADD COLUMN transfer_term TEXT
  CHECK(transfer_term IS NULL OR length(trim(transfer_term))>0);
ALTER TABLE transfer_cases ADD COLUMN current_school_state_snapshot TEXT;
ALTER TABLE transfer_cases ADD COLUMN current_school_region_snapshot TEXT;
ALTER TABLE transfer_cases ADD COLUMN current_school_type_snapshot TEXT;

ALTER TABLE transfer_school_applications ADD COLUMN branch_uid TEXT
  CHECK(branch_uid IS NULL OR length(trim(branch_uid)) BETWEEN 1 AND 160);
ALTER TABLE transfer_school_applications ADD COLUMN application_cycle_id INTEGER
  REFERENCES application_cycles(id) ON DELETE RESTRICT;
ALTER TABLE transfer_school_applications ADD COLUMN original_school_id INTEGER
  REFERENCES schools(id) ON DELETE SET NULL;
ALTER TABLE transfer_school_applications ADD COLUMN school_state_snapshot TEXT;
ALTER TABLE transfer_school_applications ADD COLUMN school_region_snapshot TEXT;
ALTER TABLE transfer_school_applications ADD COLUMN school_type_snapshot TEXT;
ALTER TABLE transfer_school_applications ADD COLUMN updated_by INTEGER
  REFERENCES users(id) ON DELETE RESTRICT;

ALTER TABLE offer_received_events ADD COLUMN transfer_branch_id INTEGER
  REFERENCES transfer_school_applications(id) ON DELETE RESTRICT;
ALTER TABLE cycle_final_acceptances ADD COLUMN transfer_branch_id INTEGER
  REFERENCES transfer_school_applications(id) ON DELETE RESTRICT;
ALTER TABLE student_documents ADD COLUMN document_date TEXT;

CREATE UNIQUE INDEX uq_application_cycles_one_active_transfer
ON application_cycles(student_id)
WHERE cycle_type='transfer' AND status='active' AND student_id IS NOT NULL;

CREATE UNIQUE INDEX uq_transfer_v2_branch_uid
ON transfer_school_applications(branch_uid)
WHERE branch_uid IS NOT NULL;

CREATE INDEX idx_transfer_v2_branches_cycle
ON transfer_school_applications(application_cycle_id,id)
WHERE application_cycle_id IS NOT NULL;

CREATE INDEX idx_offer_received_events_transfer_branch
ON offer_received_events(transfer_branch_id,received_at,id)
WHERE transfer_branch_id IS NOT NULL;

CREATE INDEX idx_cycle_final_acceptances_transfer_branch
ON cycle_final_acceptances(transfer_branch_id,acceptance_status,id)
WHERE transfer_branch_id IS NOT NULL;

CREATE TABLE transfer_v2_origin_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  previous_source_label TEXT CHECK(previous_source_label IS NULL OR previous_source_label IN (
    'aaie_existing','new_transfer'
  )),
  new_source_label TEXT NOT NULL CHECK(new_source_label IN (
    'aaie_existing','new_transfer'
  )),
  note TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  CHECK(previous_source_label IS NULL OR previous_source_label<>new_source_label)
);

CREATE TABLE transfer_target_state_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL
    REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
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

CREATE INDEX idx_transfer_target_state_current
ON transfer_target_state_events(branch_id,occurred_at DESC,id DESC);

CREATE TABLE transfer_document_requirements (
  id INTEGER PRIMARY KEY,
  requirement_uid TEXT NOT NULL UNIQUE CHECK(length(trim(requirement_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL
    REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  document_type TEXT NOT NULL CHECK(length(trim(document_type))>0),
  label TEXT NOT NULL CHECK(length(trim(label))>0),
  is_required INTEGER NOT NULL DEFAULT 1 CHECK(is_required IN (0,1)),
  requires_current_cycle INTEGER NOT NULL DEFAULT 0 CHECK(requires_current_cycle IN (0,1)),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL CHECK(length(trim(updated_at))>0)
);

CREATE UNIQUE INDEX uq_transfer_document_requirement_active
ON transfer_document_requirements(branch_id,document_type)
WHERE status='active';

CREATE TABLE transfer_document_bindings (
  id INTEGER PRIMARY KEY,
  binding_uid TEXT NOT NULL UNIQUE CHECK(length(trim(binding_uid)) BETWEEN 1 AND 160),
  document_id INTEGER NOT NULL REFERENCES student_documents(id) ON DELETE RESTRICT,
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE RESTRICT,
  source_cycle_id INTEGER REFERENCES application_cycles(id) ON DELETE RESTRICT,
  current_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  document_type TEXT NOT NULL CHECK(length(trim(document_type))>0),
  document_date TEXT,
  initial_validity TEXT NOT NULL DEFAULT 'valid'
    CHECK(initial_validity IN ('valid','invalid','not_adopted')),
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  UNIQUE(current_cycle_id,document_id,branch_id,document_type)
);

CREATE INDEX idx_transfer_document_bindings_current
ON transfer_document_bindings(current_cycle_id,branch_id,document_type,document_date DESC,id DESC);

CREATE TABLE transfer_document_binding_state_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  binding_id INTEGER NOT NULL REFERENCES transfer_document_bindings(id) ON DELETE RESTRICT,
  previous_state TEXT CHECK(previous_state IS NULL OR previous_state IN (
    'valid','invalid','not_adopted'
  )),
  new_state TEXT NOT NULL CHECK(new_state IN ('valid','invalid','not_adopted')),
  reason TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);

CREATE INDEX idx_transfer_document_binding_state_current
ON transfer_document_binding_state_events(binding_id,occurred_at DESC,id DESC);

CREATE TABLE transfer_document_adoption_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  requirement_id INTEGER NOT NULL REFERENCES transfer_document_requirements(id) ON DELETE RESTRICT,
  binding_id INTEGER REFERENCES transfer_document_bindings(id) ON DELETE RESTRICT,
  action TEXT NOT NULL CHECK(action IN ('adopted','unadopted','auto_fallback')),
  reason TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  CHECK(action='unadopted' OR binding_id IS NOT NULL)
);

CREATE INDEX idx_transfer_document_adoption_current
ON transfer_document_adoption_events(requirement_id,occurred_at DESC,id DESC);

CREATE TABLE transfer_payment_aggregates (
  id INTEGER PRIMARY KEY,
  payment_uid TEXT NOT NULL UNIQUE CHECK(length(trim(payment_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  final_acceptance_id INTEGER NOT NULL REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  accepted_offer_event_id INTEGER NOT NULL REFERENCES offer_received_events(id) ON DELETE RESTRICT,
  paid_total REAL CHECK(paid_total IS NULL OR paid_total>=0),
  last_payment_date TEXT,
  paid_status TEXT NOT NULL DEFAULT 'unpaid' CHECK(paid_status IN ('unpaid','paid')),
  payment_proof_status TEXT NOT NULL DEFAULT 'missing'
    CHECK(payment_proof_status IN ('missing','provided','invalid')),
  inherited_from_payment_id INTEGER REFERENCES transfer_payment_aggregates(id) ON DELETE RESTRICT,
  created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL CHECK(length(trim(updated_at))>0),
  UNIQUE(final_acceptance_id)
);

CREATE INDEX idx_transfer_payment_cycle_branch
ON transfer_payment_aggregates(application_cycle_id,branch_id,id);

CREATE TABLE transfer_payment_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  payment_id INTEGER NOT NULL REFERENCES transfer_payment_aggregates(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL CHECK(event_type IN (
    'created','paid_confirmed','payment_updated','payment_inherited','payment_reset',
    'proof_attached','proof_inherited','proof_invalidated'
  )),
  before_json TEXT,
  after_json TEXT,
  reason TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);

CREATE INDEX idx_transfer_payment_events_current
ON transfer_payment_events(payment_id,occurred_at DESC,id DESC);

CREATE TABLE transfer_coe_confirmations (
  id INTEGER PRIMARY KEY,
  confirmation_uid TEXT NOT NULL UNIQUE CHECK(length(trim(confirmation_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER NOT NULL REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  final_acceptance_id INTEGER NOT NULL REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  coe_oshc_id INTEGER NOT NULL REFERENCES coe_oshc(id) ON DELETE RESTRICT,
  document_id INTEGER REFERENCES student_documents(id) ON DELETE RESTRICT,
  issued_date TEXT NOT NULL CHECK(length(trim(issued_date))>0),
  status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed','cancelled')),
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL CHECK(length(trim(confirmed_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);

CREATE UNIQUE INDEX uq_transfer_coe_one_confirmed_acceptance
ON transfer_coe_confirmations(final_acceptance_id)
WHERE status='confirmed';

CREATE TABLE transfer_coe_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  confirmation_id INTEGER NOT NULL REFERENCES transfer_coe_confirmations(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL CHECK(event_type IN ('confirmed','cancelled')),
  reason TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);

CREATE TABLE transfer_v2_lifecycle_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  final_acceptance_id INTEGER REFERENCES cycle_final_acceptances(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL CHECK(event_type IN (
    'waiting_coe_started','application_complete_confirmed','cycle_cancelled','cycle_completed'
  )),
  note TEXT,
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  CHECK(
    event_type NOT IN ('waiting_coe_started','application_complete_confirmed')
    OR final_acceptance_id IS NOT NULL
  )
);

CREATE INDEX idx_transfer_v2_lifecycle_current
ON transfer_v2_lifecycle_events(application_cycle_id,occurred_at DESC,id DESC);

CREATE TABLE transfer_v2_command_receipts (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL
    REFERENCES application_cycles(id) ON DELETE RESTRICT,
  command_type TEXT NOT NULL CHECK(length(trim(command_type))>0),
  aggregate_type TEXT NOT NULL CHECK(length(trim(aggregate_type))>0),
  aggregate_id INTEGER NOT NULL,
  request_json TEXT NOT NULL CHECK(length(trim(request_json))>0),
  actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  occurred_at TEXT NOT NULL CHECK(length(trim(occurred_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0)
);

CREATE INDEX idx_transfer_v2_command_receipts_cycle
ON transfer_v2_command_receipts(application_cycle_id,occurred_at,id);

CREATE TABLE transfer_completion_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE CHECK(length(trim(event_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  final_branch_id INTEGER NOT NULL REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  final_school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
  final_school_name_snapshot TEXT NOT NULL CHECK(length(trim(final_school_name_snapshot))>0),
  application_year_level TEXT NOT NULL CHECK(length(trim(application_year_level))>0),
  transfer_year INTEGER NOT NULL CHECK(transfer_year BETWEEN 2000 AND 2200),
  transfer_term TEXT NOT NULL CHECK(length(trim(transfer_term))>0),
  coe_status TEXT NOT NULL CHECK(coe_status='confirmed'),
  update_current_school INTEGER NOT NULL CHECK(update_current_school IN (0,1)),
  update_current_year_level INTEGER NOT NULL CHECK(update_current_year_level IN (0,1)),
  completed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  completed_at TEXT NOT NULL CHECK(length(trim(completed_at))>0),
  created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
  UNIQUE(application_cycle_id)
);

CREATE TABLE transfer_school_library_requests (
  id INTEGER PRIMARY KEY,
  request_uid TEXT NOT NULL UNIQUE CHECK(length(trim(request_uid)) BETWEEN 1 AND 160),
  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id) ON DELETE RESTRICT,
  branch_id INTEGER REFERENCES transfer_school_applications(id) ON DELETE RESTRICT,
  proposed_name TEXT NOT NULL CHECK(length(trim(proposed_name))>0),
  proposed_state TEXT,
  proposed_region TEXT,
  proposed_school_type TEXT,
  duplicate_confirmation_reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  requested_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  requested_at TEXT NOT NULL CHECK(length(trim(requested_at))>0),
  decided_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  decided_at TEXT,
  decision_note TEXT,
  CHECK(
    (status='pending' AND decided_by IS NULL AND decided_at IS NULL)
    OR (status IN ('approved','rejected') AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
  )
);

CREATE TRIGGER transfer_v2_contract_required_insert
BEFORE INSERT ON transfer_cases
WHEN NEW.v2_version=2 AND NOT EXISTS (
  SELECT 1
  FROM contracts AS contract
  JOIN student_cases AS student_case ON student_case.id=NEW.case_id
  WHERE contract.id=NEW.source_contract_id
    AND contract.student_id=student_case.student_id
    AND contract.contract_type='TransferApplicationContract'
    AND contract.contract_status='FullyExecuted'
    AND contract.deleted_at IS NULL
    AND contract.voided_at IS NULL
    AND contract.final_signed_file_path IS NOT NULL
    AND length(trim(contract.final_signed_file_path))>0
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 requires a fully executed transfer contract');
END;

CREATE TRIGGER transfer_v2_contract_required_update
BEFORE UPDATE OF case_id,source_contract_id,v2_version ON transfer_cases
WHEN NEW.v2_version=2 AND NOT EXISTS (
  SELECT 1
  FROM contracts AS contract
  JOIN student_cases AS student_case ON student_case.id=NEW.case_id
  WHERE contract.id=NEW.source_contract_id
    AND contract.student_id=student_case.student_id
    AND contract.contract_type='TransferApplicationContract'
    AND contract.contract_status='FullyExecuted'
    AND contract.deleted_at IS NULL
    AND contract.voided_at IS NULL
    AND contract.final_signed_file_path IS NOT NULL
    AND length(trim(contract.final_signed_file_path))>0
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 requires a fully executed transfer contract');
END;

CREATE TRIGGER transfer_v2_branch_ownership_insert
BEFORE INSERT ON transfer_school_applications
WHEN NEW.application_cycle_id IS NOT NULL AND (
  NEW.branch_uid IS NULL OR length(trim(NEW.branch_uid))=0 OR NOT EXISTS (
    SELECT 1
    FROM application_cycles AS cycle
    JOIN transfer_cases AS transfer_case ON transfer_case.case_id=NEW.case_id
    WHERE cycle.id=NEW.application_cycle_id
      AND cycle.source_student_case_id=NEW.case_id
      AND cycle.cycle_type='transfer'
      AND transfer_case.v2_version=2
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 branch ownership mismatch');
END;

CREATE TRIGGER transfer_v2_branch_ownership_update
BEFORE UPDATE OF case_id,application_cycle_id,branch_uid ON transfer_school_applications
WHEN NEW.application_cycle_id IS NOT NULL AND (
  NEW.branch_uid IS NULL OR length(trim(NEW.branch_uid))=0 OR NOT EXISTS (
    SELECT 1
    FROM application_cycles AS cycle
    JOIN transfer_cases AS transfer_case ON transfer_case.case_id=NEW.case_id
    WHERE cycle.id=NEW.application_cycle_id
      AND cycle.source_student_case_id=NEW.case_id
      AND cycle.cycle_type='transfer'
      AND transfer_case.v2_version=2
  )
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 branch ownership mismatch');
END;

CREATE TRIGGER transfer_v2_offer_branch_insert
BEFORE INSERT ON offer_received_events
WHEN NEW.transfer_branch_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM transfer_school_applications AS branch
  WHERE branch.id=NEW.transfer_branch_id
    AND branch.application_cycle_id=NEW.application_cycle_id
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 Offer branch ownership mismatch');
END;

CREATE TRIGGER transfer_v2_offer_branch_update
BEFORE UPDATE OF application_cycle_id,transfer_branch_id ON offer_received_events
WHEN NEW.transfer_branch_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM transfer_school_applications AS branch
  WHERE branch.id=NEW.transfer_branch_id
    AND branch.application_cycle_id=NEW.application_cycle_id
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 Offer branch ownership mismatch');
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

CREATE TRIGGER transfer_v2_payment_ownership_insert
BEFORE INSERT ON transfer_payment_aggregates
WHEN NOT EXISTS (
  SELECT 1 FROM cycle_final_acceptances AS acceptance
  WHERE acceptance.id=NEW.final_acceptance_id
    AND acceptance.application_cycle_id=NEW.application_cycle_id
    AND acceptance.transfer_branch_id=NEW.branch_id
    AND acceptance.offer_received_event_id=NEW.accepted_offer_event_id
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 payment ownership mismatch');
END;

CREATE TRIGGER transfer_v2_coe_ownership_insert
BEFORE INSERT ON transfer_coe_confirmations
WHEN NOT EXISTS (
  SELECT 1 FROM cycle_final_acceptances AS acceptance
  WHERE acceptance.id=NEW.final_acceptance_id
    AND acceptance.application_cycle_id=NEW.application_cycle_id
    AND acceptance.transfer_branch_id=NEW.branch_id
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 COE ownership mismatch');
END;

CREATE TRIGGER transfer_v2_lifecycle_acceptance_ownership_insert
BEFORE INSERT ON transfer_v2_lifecycle_events
WHEN NEW.final_acceptance_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM cycle_final_acceptances AS acceptance
  WHERE acceptance.id=NEW.final_acceptance_id
    AND acceptance.application_cycle_id=NEW.application_cycle_id
    AND (NEW.branch_id IS NULL OR acceptance.transfer_branch_id=NEW.branch_id)
)
BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 lifecycle acceptance ownership mismatch');
END;

CREATE TRIGGER transfer_v2_origin_events_no_update
BEFORE UPDATE ON transfer_v2_origin_events BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 origin events are append-only');
END;
CREATE TRIGGER transfer_v2_origin_events_no_delete
BEFORE DELETE ON transfer_v2_origin_events BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 origin events are append-only');
END;

CREATE TRIGGER transfer_target_state_events_no_update
BEFORE UPDATE ON transfer_target_state_events BEGIN
  SELECT RAISE(ABORT, 'Transfer target state events are append-only');
END;
CREATE TRIGGER transfer_target_state_events_no_delete
BEFORE DELETE ON transfer_target_state_events BEGIN
  SELECT RAISE(ABORT, 'Transfer target state events are append-only');
END;

CREATE TRIGGER transfer_document_bindings_no_update
BEFORE UPDATE ON transfer_document_bindings BEGIN
  SELECT RAISE(ABORT, 'Transfer document bindings are append-only');
END;
CREATE TRIGGER transfer_document_bindings_no_delete
BEFORE DELETE ON transfer_document_bindings BEGIN
  SELECT RAISE(ABORT, 'Transfer document bindings are append-only');
END;
CREATE TRIGGER transfer_document_binding_state_events_no_update
BEFORE UPDATE ON transfer_document_binding_state_events BEGIN
  SELECT RAISE(ABORT, 'Transfer document binding state events are append-only');
END;
CREATE TRIGGER transfer_document_binding_state_events_no_delete
BEFORE DELETE ON transfer_document_binding_state_events BEGIN
  SELECT RAISE(ABORT, 'Transfer document binding state events are append-only');
END;
CREATE TRIGGER transfer_document_adoption_events_no_update
BEFORE UPDATE ON transfer_document_adoption_events BEGIN
  SELECT RAISE(ABORT, 'Transfer document adoption events are append-only');
END;
CREATE TRIGGER transfer_document_adoption_events_no_delete
BEFORE DELETE ON transfer_document_adoption_events BEGIN
  SELECT RAISE(ABORT, 'Transfer document adoption events are append-only');
END;

CREATE TRIGGER transfer_payment_events_no_update
BEFORE UPDATE ON transfer_payment_events BEGIN
  SELECT RAISE(ABORT, 'Transfer payment events are append-only');
END;
CREATE TRIGGER transfer_payment_events_no_delete
BEFORE DELETE ON transfer_payment_events BEGIN
  SELECT RAISE(ABORT, 'Transfer payment events are append-only');
END;
CREATE TRIGGER transfer_coe_events_no_update
BEFORE UPDATE ON transfer_coe_events BEGIN
  SELECT RAISE(ABORT, 'Transfer COE events are append-only');
END;
CREATE TRIGGER transfer_coe_events_no_delete
BEFORE DELETE ON transfer_coe_events BEGIN
  SELECT RAISE(ABORT, 'Transfer COE events are append-only');
END;
CREATE TRIGGER transfer_v2_lifecycle_events_no_update
BEFORE UPDATE ON transfer_v2_lifecycle_events BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 lifecycle events are append-only');
END;
CREATE TRIGGER transfer_v2_lifecycle_events_no_delete
BEFORE DELETE ON transfer_v2_lifecycle_events BEGIN
  SELECT RAISE(ABORT, 'Transfer V2 lifecycle events are append-only');
END;
CREATE TRIGGER transfer_completion_events_no_update
BEFORE UPDATE ON transfer_completion_events BEGIN
  SELECT RAISE(ABORT, 'Transfer completion events are append-only');
END;
CREATE TRIGGER transfer_completion_events_no_delete
BEFORE DELETE ON transfer_completion_events BEGIN
  SELECT RAISE(ABORT, 'Transfer completion events are append-only');
END;
