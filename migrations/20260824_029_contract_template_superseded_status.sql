-- migration_mode: sqlite_fk_off_rebuild

-- Superseded is a protected terminal state for mutable draft ancestry only.
-- Published and Historical retain their existing immutable provenance contract.

DROP TRIGGER contract_template_versions_status_immutable_insert;
DROP TRIGGER contract_template_versions_status_immutable_update;
DROP TRIGGER contract_template_versions_no_replace_insert;
DROP TRIGGER contract_template_versions_no_replace_update;
DROP TRIGGER contract_template_versions_immutable_content;
DROP TRIGGER contract_template_versions_no_delete;
DROP INDEX idx_contract_template_versions_identity_status;

CREATE TABLE contract_template_versions_rebuild_029 (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL REFERENCES contract_template_identities(id) ON DELETE RESTRICT,
  version_number INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('Draft','Simulation','Validation','Preview','Superseded','Published','Historical')),
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

INSERT INTO contract_template_versions_rebuild_029(
  id,identity_id,version_number,status,source_version_id,revision,
  snapshot_json,content_hash,immutable,created_by,created_at,
  published_by,published_at
)
SELECT
  id,identity_id,version_number,status,source_version_id,revision,
  snapshot_json,content_hash,immutable,created_by,created_at,
  published_by,published_at
FROM contract_template_versions;

ALTER TABLE contract_template_versions
RENAME TO contract_template_versions_legacy_029;
ALTER TABLE contract_template_versions_rebuild_029
RENAME TO contract_template_versions;
DROP TABLE contract_template_versions_legacy_029;

CREATE INDEX idx_contract_template_versions_identity_status
ON contract_template_versions(identity_id, status, version_number DESC);

CREATE TRIGGER contract_template_versions_status_immutable_insert
BEFORE INSERT ON contract_template_versions
WHEN NEW.status='Superseded' OR NEW.status IN ('Published','Historical') OR (
  NEW.status NOT IN ('Superseded','Published','Historical')
  AND (NEW.immutable IS NOT 0 OR NEW.published_by IS NOT NULL OR NEW.published_at IS NOT NULL)
)
BEGIN
  SELECT RAISE(ABORT, 'protected template terminal states must be reached through a valid transition');
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
  NEW.status='Superseded'
  AND NEW.status IS NOT OLD.status
  AND NOT (
    OLD.status IN ('Draft','Simulation','Validation','Preview')
    AND OLD.immutable=0
    AND OLD.published_by IS NULL
    AND OLD.published_at IS NULL
  )
) OR (
  OLD.status='Superseded'
  AND NEW.status IS NOT OLD.status
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
  SELECT RAISE(ABORT, 'template status transition violates immutable provenance or protected Superseded terminal rules');
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

CREATE TRIGGER contract_template_versions_supersede_status_only
BEFORE UPDATE ON contract_template_versions
WHEN NEW.status='Superseded'
  AND NEW.status IS NOT OLD.status
  AND (
    NEW.id IS NOT OLD.id
    OR NEW.identity_id IS NOT OLD.identity_id
    OR NEW.version_number IS NOT OLD.version_number
    OR NEW.source_version_id IS NOT OLD.source_version_id
    OR NEW.revision IS NOT OLD.revision
    OR NEW.snapshot_json IS NOT OLD.snapshot_json
    OR NEW.content_hash IS NOT OLD.content_hash
    OR NEW.immutable IS NOT OLD.immutable
    OR NEW.created_by IS NOT OLD.created_by
    OR NEW.created_at IS NOT OLD.created_at
    OR NEW.published_by IS NOT OLD.published_by
    OR NEW.published_at IS NOT OLD.published_at
  )
BEGIN
  SELECT RAISE(ABORT, 'superseding a contract template version may change status only');
END;

CREATE TRIGGER contract_template_versions_superseded_terminal
BEFORE UPDATE ON contract_template_versions
WHEN OLD.status='Superseded'
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template version is terminal and cannot be rewritten');
END;

CREATE TRIGGER contract_template_versions_no_delete
BEFORE DELETE ON contract_template_versions
BEGIN
  SELECT RAISE(ABORT, 'contract template versions are permanent');
END;

CREATE TRIGGER contract_template_blocks_superseded_insert
BEFORE INSERT ON contract_template_clause_blocks
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE (id=NEW.version_id OR id IN (
    SELECT version_id FROM contract_template_clause_blocks WHERE id=NEW.id
  )) AND status='Superseded'
)
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template blocks are immutable');
END;

CREATE TRIGGER contract_template_blocks_superseded_update
BEFORE UPDATE ON contract_template_clause_blocks
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id IN (OLD.version_id,NEW.version_id) AND status='Superseded'
)
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template blocks are immutable');
END;

CREATE TRIGGER contract_template_blocks_superseded_delete
BEFORE DELETE ON contract_template_clause_blocks
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id=OLD.version_id AND status='Superseded'
)
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template blocks are immutable');
END;

CREATE TRIGGER contract_template_services_superseded_insert
BEFORE INSERT ON contract_template_service_items
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE (id=NEW.version_id OR id IN (
    SELECT version_id FROM contract_template_service_items WHERE id=NEW.id
  )) AND status='Superseded'
)
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template services are immutable');
END;

CREATE TRIGGER contract_template_services_superseded_update
BEFORE UPDATE ON contract_template_service_items
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id IN (OLD.version_id,NEW.version_id) AND status='Superseded'
)
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template services are immutable');
END;

CREATE TRIGGER contract_template_services_superseded_delete
BEFORE DELETE ON contract_template_service_items
WHEN EXISTS (
  SELECT 1 FROM contract_template_versions
  WHERE id=OLD.version_id AND status='Superseded'
)
BEGIN
  SELECT RAISE(ABORT, 'superseded contract template services are immutable');
END;
