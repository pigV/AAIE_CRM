SELECT count(*) = 1
FROM sqlite_master
WHERE type='table'
  AND name='contract_template_versions'
  AND sql LIKE '%''Superseded''%'
  AND sql LIKE '%''Published''%'
  AND sql LIKE '%''Historical''%';

SELECT count(*) = 0
FROM sqlite_master
WHERE name IN (
  'contract_template_versions_rebuild_029',
  'contract_template_versions_legacy_029'
);

SELECT count(*) = 13
FROM pragma_table_info('contract_template_versions');

SELECT count(*) = 2
FROM pragma_foreign_key_list('contract_template_versions');

SELECT count(*) = 1
FROM sqlite_master
WHERE type='index'
  AND name='idx_contract_template_versions_identity_status';

SELECT count(*) = 14
FROM sqlite_master
WHERE type='trigger'
  AND name IN (
    'contract_template_versions_status_immutable_insert',
    'contract_template_versions_status_immutable_update',
    'contract_template_versions_no_replace_insert',
    'contract_template_versions_no_replace_update',
    'contract_template_versions_immutable_content',
    'contract_template_versions_supersede_status_only',
    'contract_template_versions_superseded_terminal',
    'contract_template_versions_no_delete',
    'contract_template_blocks_superseded_insert',
    'contract_template_blocks_superseded_update',
    'contract_template_blocks_superseded_delete',
    'contract_template_services_superseded_insert',
    'contract_template_services_superseded_update',
    'contract_template_services_superseded_delete'
  );

SELECT count(*) = 1
FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_versions_supersede_status_only'
  AND sql LIKE '%NEW.snapshot_json IS NOT OLD.snapshot_json%'
  AND sql LIKE '%NEW.source_version_id IS NOT OLD.source_version_id%'
  AND sql LIKE '%NEW.created_at IS NOT OLD.created_at%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_versions_status_immutable_update'
  AND sql LIKE '%OLD.status IN (''Draft'',''Simulation'',''Validation'',''Preview'')%'
  AND sql LIKE '%OLD.status=''Superseded''%'
  AND sql LIKE '%NEW.status=''Historical''%';

SELECT count(*) = 0
FROM contract_template_versions
WHERE (
    status IN ('Published','Historical')
    AND (
      immutable IS NOT 1
      OR published_by IS NULL
      OR published_at IS NULL
    )
  ) OR (
    status NOT IN ('Published','Historical')
    AND (
      immutable IS NOT 0
      OR published_by IS NOT NULL
      OR published_at IS NOT NULL
    )
  );

SELECT count(*) = 0
FROM contract_template_identities AS identity
JOIN contract_template_versions AS version
  ON version.id=identity.current_published_version_id
WHERE version.status<>'Published'
   OR version.immutable<>1
   OR version.published_by IS NULL
   OR version.published_at IS NULL;

SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
