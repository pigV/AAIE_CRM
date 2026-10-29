-- Contract Template Editor V1. Editor-only schema; no existing table is altered.

CREATE TABLE contract_template_identities (
  id INTEGER PRIMARY KEY,
  template_type TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  current_published_version_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE contract_template_versions (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES contract_template_identities(id) ON DELETE RESTRICT,
  version_number INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('Draft','Simulation','Validation','Preview','Published','Historical')),
  source_version_id INTEGER REFERENCES contract_template_versions(id) ON DELETE RESTRICT,
  revision INTEGER NOT NULL DEFAULT 1,
  snapshot_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 0 CHECK(immutable IN (0,1)),
  created_by INTEGER,
  created_at TEXT NOT NULL,
  published_by INTEGER,
  published_at TEXT,
  UNIQUE(identity_id, version_number),
  UNIQUE(id, identity_id)
);

CREATE TABLE contract_template_clause_blocks (
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES contract_template_versions(id) ON DELETE RESTRICT,
  block_key TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  fixed_layout INTEGER NOT NULL DEFAULT 0 CHECK(fixed_layout IN (0,1)),
  required INTEGER NOT NULL DEFAULT 0 CHECK(required IN (0,1)),
  legal_kind TEXT,
  required_variables_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(version_id, block_key),
  UNIQUE(version_id, sort_order)
);

CREATE TABLE contract_template_service_items (
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES contract_template_versions(id) ON DELETE RESTRICT,
  service_id TEXT NOT NULL,
  label TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  default_selected INTEGER NOT NULL DEFAULT 0 CHECK(default_selected IN (0,1)),
  UNIQUE(version_id, service_id),
  UNIQUE(version_id, sort_order)
);

CREATE TABLE contract_template_simulations (
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL,
  template_identity_id INTEGER NOT NULL REFERENCES contract_template_identities(id) ON DELETE RESTRICT,
  template_revision INTEGER NOT NULL,
  template_content_hash TEXT NOT NULL,
  template_snapshot_hash TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('system','student_readonly')),
  input_json TEXT NOT NULL,
  simulation_input_hash TEXT NOT NULL,
  signer_json TEXT NOT NULL,
  selected_services_json TEXT NOT NULL,
  authority_content_hash TEXT NOT NULL,
  docx_content_hash TEXT NOT NULL,
  pdf_content_hash TEXT NOT NULL,
  signing_content_hash TEXT NOT NULL,
  docx_semantic_hash TEXT NOT NULL,
  pdf_semantic_hash TEXT NOT NULL,
  signing_semantic_hash TEXT NOT NULL,
  docx_semantic_trace_json TEXT NOT NULL,
  pdf_semantic_trace_json TEXT NOT NULL,
  signing_semantic_trace_json TEXT NOT NULL,
  docx_artifact_hash TEXT NOT NULL,
  pdf_artifact_hash TEXT NOT NULL,
  signing_artifact_hash TEXT NOT NULL,
  artifact_manifest_json TEXT NOT NULL,
  simulation_result_hash TEXT NOT NULL,
  artifact_root TEXT NOT NULL,
  artifact_dir TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('Passed','Failed')),
  error_text TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  simulated_at TEXT NOT NULL,
  FOREIGN KEY(version_id, template_identity_id)
    REFERENCES contract_template_versions(id, identity_id) ON DELETE RESTRICT
);

CREATE TABLE contract_template_validations (
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES contract_template_versions(id) ON DELETE RESTRICT,
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  issues_json TEXT NOT NULL,
  validated_by INTEGER,
  validated_at TEXT NOT NULL
);

CREATE TABLE contract_template_edit_locks (
  identity_id INTEGER PRIMARY KEY REFERENCES contract_template_identities(id) ON DELETE RESTRICT,
  version_id INTEGER NOT NULL REFERENCES contract_template_versions(id) ON DELETE RESTRICT,
  owner_user_id INTEGER NOT NULL,
  token TEXT NOT NULL UNIQUE,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE contract_template_audit_events (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER REFERENCES contract_template_identities(id) ON DELETE RESTRICT,
  version_id INTEGER REFERENCES contract_template_versions(id) ON DELETE RESTRICT,
  actor_user_id INTEGER,
  action TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE contract_template_permissions (
  user_id INTEGER PRIMARY KEY,
  can_edit INTEGER NOT NULL DEFAULT 0 CHECK(can_edit IN (0,1)),
  can_publish INTEGER NOT NULL DEFAULT 0 CHECK(can_publish IN (0,1)),
  granted_by INTEGER NOT NULL,
  granted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_contract_template_versions_identity_status
ON contract_template_versions(identity_id, status, version_number DESC);
CREATE INDEX idx_contract_template_simulations_version
ON contract_template_simulations(version_id, created_at DESC);
CREATE INDEX idx_contract_template_validations_version
ON contract_template_validations(version_id, validated_at DESC);
CREATE INDEX idx_contract_template_audit_target
ON contract_template_audit_events(identity_id, version_id, created_at DESC);

CREATE TRIGGER contract_template_versions_status_immutable_insert
BEFORE INSERT ON contract_template_versions
WHEN NEW.status IN ('Published','Historical') OR (
  NEW.status NOT IN ('Published','Historical')
  AND (NEW.immutable IS NOT 0 OR NEW.published_by IS NOT NULL OR NEW.published_at IS NOT NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'published or historical template versions must be reached through the immutable draft transition');
END;

CREATE TRIGGER contract_template_versions_status_immutable_update
BEFORE UPDATE ON contract_template_versions
WHEN (
  NEW.status IN ('Published','Historical')
  AND (NEW.immutable IS NOT 1 OR NEW.published_by IS NULL OR NEW.published_at IS NULL)
) OR (
  NEW.status NOT IN ('Published','Historical')
    AND (NEW.immutable IS NOT 0 OR NEW.published_by IS NOT NULL OR NEW.published_at IS NOT NULL)
  ) OR (
    NEW.status='Historical'
    AND NEW.status IS NOT OLD.status
    AND NOT (OLD.status='Published' AND OLD.immutable=1)
  ) OR (
    OLD.status='Published'
    AND NEW.status='Historical'
    AND EXISTS (
      SELECT 1 FROM contract_template_identities
      WHERE current_published_version_id=OLD.id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'published or historical template versions must be immutable with publication provenance');
END;

CREATE TRIGGER contract_template_versions_no_replace_insert
BEFORE INSERT ON contract_template_versions
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id=NEW.id
     OR (identity_id=NEW.identity_id AND version_number=NEW.version_number)
)
BEGIN
  SELECT RAISE(ABORT, 'permanent contract template version cannot be replaced');
END;

CREATE TRIGGER contract_template_versions_no_replace_update
BEFORE UPDATE ON contract_template_versions
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions AS version
  WHERE version.id<>OLD.id
    AND (
      version.id=NEW.id
      OR (version.identity_id=NEW.identity_id AND version.version_number=NEW.version_number)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'permanent contract template version cannot be replaced');
END;

CREATE TRIGGER contract_template_versions_immutable_content
BEFORE UPDATE ON contract_template_versions
WHEN OLD.immutable=1 AND (
  NEW.id IS NOT OLD.id
  OR NEW.immutable IS NOT 1
  OR NEW.identity_id IS NOT OLD.identity_id
  OR NEW.version_number IS NOT OLD.version_number
  OR NEW.snapshot_json IS NOT OLD.snapshot_json
  OR NEW.content_hash IS NOT OLD.content_hash
  OR NEW.revision IS NOT OLD.revision
  OR NEW.source_version_id IS NOT OLD.source_version_id
  OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.published_by IS NOT OLD.published_by
  OR NEW.published_at IS NOT OLD.published_at
  OR (
    NEW.status IS NOT OLD.status
    AND NOT (OLD.status='Published' AND NEW.status='Historical')
  )
)
BEGIN
  SELECT RAISE(ABORT, 'immutable contract template version cannot be rewritten');
END;

CREATE TRIGGER contract_template_versions_no_delete
BEFORE DELETE ON contract_template_versions
BEGIN
  SELECT RAISE(ABORT, 'contract template versions are permanent');
END;

CREATE TRIGGER contract_template_identities_current_published_immutable
BEFORE UPDATE OF current_published_version_id ON contract_template_identities
WHEN (
  OLD.current_published_version_id IS NOT NULL
  AND NEW.current_published_version_id IS NULL
) OR (
  NEW.current_published_version_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM contract_template_versions
    WHERE id=NEW.current_published_version_id
      AND identity_id=NEW.id
      AND status='Published'
      AND immutable=1
      AND published_by IS NOT NULL
      AND published_at IS NOT NULL
  )
)
BEGIN
  SELECT RAISE(ABORT, 'current published template must be an immutable published version');
END;

CREATE TRIGGER contract_template_identities_current_published_insert
BEFORE INSERT ON contract_template_identities
WHEN NEW.current_published_version_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'new contract template identity must not set a current published version');
END;

CREATE TRIGGER contract_template_identities_no_replace_insert
BEFORE INSERT ON contract_template_identities
WHEN EXISTS (
  SELECT 1 FROM contract_template_identities
  WHERE id=NEW.id OR template_type=NEW.template_type
)
BEGIN
  SELECT RAISE(ABORT, 'permanent contract template identity cannot be replaced');
END;

CREATE TRIGGER contract_template_identities_no_replace_update
BEFORE UPDATE ON contract_template_identities
WHEN EXISTS (
  SELECT 1 FROM contract_template_identities AS identity
  WHERE identity.id<>OLD.id
    AND (identity.id=NEW.id OR identity.template_type=NEW.template_type)
)
BEGIN
  SELECT RAISE(ABORT, 'permanent contract template identity cannot be replaced');
END;

CREATE TRIGGER contract_template_identities_immutable_fields
BEFORE UPDATE ON contract_template_identities
WHEN NEW.id IS NOT OLD.id
  OR NEW.template_type IS NOT OLD.template_type
  OR NEW.display_name IS NOT OLD.display_name
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'immutable contract template identity fields cannot be rewritten');
END;

CREATE TRIGGER contract_template_identities_no_delete
BEFORE DELETE ON contract_template_identities
BEGIN
  SELECT RAISE(ABORT, 'permanent contract template identities cannot be deleted');
END;

CREATE TRIGGER contract_template_blocks_immutable_update
BEFORE UPDATE ON contract_template_clause_blocks
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id IN (OLD.version_id, NEW.version_id) AND immutable=1
)
BEGIN
  SELECT RAISE(ABORT, 'published contract template blocks are immutable');
END;

CREATE TRIGGER contract_template_blocks_immutable_insert
BEFORE INSERT ON contract_template_clause_blocks
WHEN EXISTS (SELECT 1 FROM contract_template_versions WHERE id=NEW.version_id AND immutable=1)
BEGIN
  SELECT RAISE(ABORT, 'published contract template blocks are immutable');
END;

CREATE TRIGGER contract_template_blocks_immutable_no_replace_insert
BEFORE INSERT ON contract_template_clause_blocks
WHEN EXISTS (
  SELECT 1 FROM contract_template_clause_blocks AS block
  JOIN contract_template_versions AS version ON version.id=block.version_id
  WHERE block.id=NEW.id AND version.immutable=1
)
BEGIN
  SELECT RAISE(ABORT, 'immutable contract template block cannot be replaced');
END;

CREATE TRIGGER contract_template_blocks_immutable_no_replace_update
BEFORE UPDATE ON contract_template_clause_blocks
WHEN EXISTS (
  SELECT 1 FROM contract_template_clause_blocks AS block
  JOIN contract_template_versions AS version ON version.id=block.version_id
  WHERE block.id<>OLD.id
    AND version.immutable=1
    AND (
      block.id=NEW.id
      OR (block.version_id=NEW.version_id AND block.block_key=NEW.block_key)
      OR (block.version_id=NEW.version_id AND block.sort_order=NEW.sort_order)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'immutable contract template block cannot be replaced');
END;

CREATE TRIGGER contract_template_blocks_immutable_delete
BEFORE DELETE ON contract_template_clause_blocks
WHEN EXISTS (SELECT 1 FROM contract_template_versions WHERE id=OLD.version_id AND immutable=1)
BEGIN
  SELECT RAISE(ABORT, 'published contract template blocks are immutable');
END;

CREATE TRIGGER contract_template_services_immutable_update
BEFORE UPDATE ON contract_template_service_items
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id IN (OLD.version_id, NEW.version_id) AND immutable=1
)
BEGIN
  SELECT RAISE(ABORT, 'published contract template services are immutable');
END;

CREATE TRIGGER contract_template_services_immutable_insert
BEFORE INSERT ON contract_template_service_items
WHEN EXISTS (SELECT 1 FROM contract_template_versions WHERE id=NEW.version_id AND immutable=1)
BEGIN
  SELECT RAISE(ABORT, 'published contract template services are immutable');
END;

CREATE TRIGGER contract_template_services_immutable_no_replace_insert
BEFORE INSERT ON contract_template_service_items
WHEN EXISTS (
  SELECT 1 FROM contract_template_service_items AS service
  JOIN contract_template_versions AS version ON version.id=service.version_id
  WHERE service.id=NEW.id AND version.immutable=1
)
BEGIN
  SELECT RAISE(ABORT, 'immutable contract template service cannot be replaced');
END;

CREATE TRIGGER contract_template_services_immutable_no_replace_update
BEFORE UPDATE ON contract_template_service_items
WHEN EXISTS (
  SELECT 1 FROM contract_template_service_items AS service
  JOIN contract_template_versions AS version ON version.id=service.version_id
  WHERE service.id<>OLD.id
    AND version.immutable=1
    AND (
      service.id=NEW.id
      OR (service.version_id=NEW.version_id AND service.service_id=NEW.service_id)
      OR (service.version_id=NEW.version_id AND service.sort_order=NEW.sort_order)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'immutable contract template service cannot be replaced');
END;

CREATE TRIGGER contract_template_services_immutable_delete
BEFORE DELETE ON contract_template_service_items
WHEN EXISTS (SELECT 1 FROM contract_template_versions WHERE id=OLD.version_id AND immutable=1)
BEGIN
  SELECT RAISE(ABORT, 'published contract template services are immutable');
END;

CREATE TRIGGER contract_template_audit_no_update
BEFORE UPDATE ON contract_template_audit_events
BEGIN
  SELECT RAISE(ABORT, 'contract template audit events are append-only');
END;

CREATE TRIGGER contract_template_audit_no_delete
BEFORE DELETE ON contract_template_audit_events
BEGIN
  SELECT RAISE(ABORT, 'contract template audit events are append-only');
END;

CREATE TRIGGER contract_template_audit_no_replace_insert
BEFORE INSERT ON contract_template_audit_events
WHEN EXISTS (SELECT 1 FROM contract_template_audit_events WHERE id=NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'contract template audit events are append-only');
END;

CREATE TRIGGER contract_template_simulations_relationship_insert
BEFORE INSERT ON contract_template_simulations
WHEN NOT EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id=NEW.version_id AND identity_id=NEW.template_identity_id
)
BEGIN
  SELECT RAISE(ABORT, 'simulation version identity relationship is invalid');
END;

CREATE TRIGGER contract_template_simulations_no_update
BEFORE UPDATE ON contract_template_simulations
BEGIN
  SELECT RAISE(ABORT, 'contract template simulation records are permanent');
END;

CREATE TRIGGER contract_template_simulations_no_delete
BEFORE DELETE ON contract_template_simulations
BEGIN
  SELECT RAISE(ABORT, 'contract template simulation records are permanent');
END;

CREATE TRIGGER contract_template_simulations_no_replace_insert
BEFORE INSERT ON contract_template_simulations
WHEN EXISTS (SELECT 1 FROM contract_template_simulations WHERE id=NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'contract template simulation records are permanent');
END;

CREATE TRIGGER contract_template_validations_no_update
BEFORE UPDATE ON contract_template_validations
BEGIN
  SELECT RAISE(ABORT, 'contract template validation records are permanent');
END;

CREATE TRIGGER contract_template_validations_no_delete
BEFORE DELETE ON contract_template_validations
BEGIN
  SELECT RAISE(ABORT, 'contract template validation records are permanent');
END;

CREATE TRIGGER contract_template_validations_no_replace_insert
BEFORE INSERT ON contract_template_validations
WHEN EXISTS (SELECT 1 FROM contract_template_validations WHERE id=NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'contract template validation records are permanent');
END;
