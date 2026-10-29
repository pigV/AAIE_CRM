SELECT count(*) = 9 FROM sqlite_master
WHERE type='table' AND name IN (
  'contract_template_identities','contract_template_versions',
  'contract_template_clause_blocks','contract_template_service_items',
  'contract_template_simulations','contract_template_validations',
  'contract_template_edit_locks','contract_template_audit_events',
  'contract_template_permissions'
);

SELECT count(*) = 18 FROM pragma_table_info('contract_template_simulations')
WHERE name IN (
  'template_identity_id','template_revision','template_content_hash',
  'template_snapshot_hash','simulation_input_hash','simulation_result_hash',
  'docx_semantic_hash','pdf_semantic_hash','signing_semantic_hash',
  'docx_semantic_trace_json','pdf_semantic_trace_json','signing_semantic_trace_json',
  'docx_artifact_hash','pdf_artifact_hash','signing_artifact_hash',
  'artifact_manifest_json','artifact_root','simulated_at'
);

SELECT count(*) = 32 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'contract_template_versions_status_immutable_insert',
  'contract_template_versions_status_immutable_update',
  'contract_template_versions_no_replace_insert',
  'contract_template_versions_no_replace_update',
  'contract_template_versions_immutable_content',
  'contract_template_versions_no_delete',
  'contract_template_identities_current_published_immutable',
  'contract_template_identities_current_published_insert',
  'contract_template_identities_no_replace_insert',
  'contract_template_identities_no_replace_update',
  'contract_template_identities_immutable_fields',
  'contract_template_identities_no_delete',
  'contract_template_blocks_immutable_insert',
  'contract_template_blocks_immutable_no_replace_insert',
  'contract_template_blocks_immutable_no_replace_update',
  'contract_template_blocks_immutable_update',
  'contract_template_blocks_immutable_delete',
  'contract_template_services_immutable_insert',
  'contract_template_services_immutable_no_replace_insert',
  'contract_template_services_immutable_no_replace_update',
  'contract_template_services_immutable_update',
  'contract_template_services_immutable_delete',
  'contract_template_audit_no_update',
  'contract_template_audit_no_delete',
  'contract_template_audit_no_replace_insert',
  'contract_template_simulations_relationship_insert',
  'contract_template_simulations_no_update',
  'contract_template_simulations_no_delete',
  'contract_template_simulations_no_replace_insert',
  'contract_template_validations_no_update',
  'contract_template_validations_no_delete',
  'contract_template_validations_no_replace_insert'
);

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_simulations_relationship_insert'
  AND sql LIKE '%version_id%'
  AND sql LIKE '%template_identity_id%';

SELECT count(*) = 6 FROM sqlite_master
WHERE type='trigger'
  AND name IN (
    'contract_template_simulations_no_update',
    'contract_template_simulations_no_delete',
    'contract_template_simulations_no_replace_insert',
    'contract_template_validations_no_update',
    'contract_template_validations_no_delete',
    'contract_template_validations_no_replace_insert'
  )
  AND sql LIKE '%permanent%';

SELECT count(*) = 2 FROM pragma_foreign_key_list('contract_template_simulations')
WHERE "from" IN ('version_id','template_identity_id')
  AND "table"='contract_template_versions';

SELECT count(*) = 0 FROM contract_template_simulations AS simulation
WHERE NOT EXISTS (
  SELECT 1 FROM contract_template_versions AS version
  WHERE version.id=simulation.version_id
    AND version.identity_id=simulation.template_identity_id
);

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_versions_immutable_content'
  AND sql LIKE '%OLD.immutable=1%'
  AND sql LIKE '%NEW.id IS NOT OLD.id%'
  AND sql LIKE '%NEW.immutable IS NOT 1%'
  AND sql LIKE '%Published%Historical%';

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger'
  AND name IN (
    'contract_template_versions_status_immutable_insert',
    'contract_template_versions_status_immutable_update'
  )
  AND sql LIKE '%Published%Historical%'
  AND sql LIKE '%immutable%';

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger'
  AND name IN (
    'contract_template_blocks_immutable_update',
    'contract_template_services_immutable_update'
  )
  AND sql LIKE '%NEW.version_id%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_identities_current_published_immutable'
  AND sql LIKE '%current_published_version_id%'
  AND sql LIKE '%status=''Published''%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_identities_current_published_immutable'
  AND sql LIKE '%OLD.current_published_version_id IS NOT NULL%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_versions_status_immutable_update'
  AND sql LIKE '%current_published_version_id=OLD.id%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_identities_current_published_insert'
  AND sql LIKE '%NEW.current_published_version_id IS NOT NULL%';

SELECT count(*) = 5 FROM sqlite_master
WHERE type='trigger'
  AND name IN (
    'contract_template_versions_no_replace_insert',
    'contract_template_identities_no_replace_insert',
    'contract_template_blocks_immutable_no_replace_insert',
    'contract_template_services_immutable_no_replace_insert',
    'contract_template_audit_no_replace_insert'
  )
  AND sql LIKE '%EXISTS%';

SELECT count(*) = 4 FROM sqlite_master
WHERE type='trigger'
  AND name IN (
    'contract_template_versions_no_replace_update',
    'contract_template_identities_no_replace_update',
    'contract_template_blocks_immutable_no_replace_update',
    'contract_template_services_immutable_no_replace_update'
  )
  AND sql LIKE '%<>OLD.id%'
  AND sql LIKE '%EXISTS%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger'
  AND name='contract_template_identities_immutable_fields'
  AND sql LIKE '%NEW.template_type IS NOT OLD.template_type%'
  AND sql LIKE '%NEW.display_name IS NOT OLD.display_name%'
  AND sql LIKE '%NEW.created_at IS NOT OLD.created_at%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='contract_template_identities_no_delete';

SELECT count(*) = 0 FROM contract_template_versions
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

SELECT count(*) = 0 FROM contract_template_identities AS identity
WHERE current_published_version_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM contract_template_versions AS version
    WHERE version.id=identity.current_published_version_id
      AND version.identity_id=identity.id
      AND version.status='Published'
      AND version.immutable=1
      AND version.published_by IS NOT NULL
      AND version.published_at IS NOT NULL
  );

SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
