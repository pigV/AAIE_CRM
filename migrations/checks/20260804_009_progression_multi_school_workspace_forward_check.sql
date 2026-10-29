SELECT count(*) = 3 FROM sqlite_master
WHERE type='table' AND name IN (
  'progression_target_applications','progression_document_links',
  'progression_case_arrangements'
);

SELECT count(*) = 1 FROM pragma_table_info('progression_case_offers')
WHERE name='target_application_id';

SELECT count(*) = 5 FROM sqlite_master
WHERE type='index' AND name IN (
  'ix_progression_target_applications_case_status',
  'uq_progression_target_applications_one_final',
  'ix_progression_document_links_case_role',
  'ix_progression_document_links_target_role',
  'uq_progression_document_links_current'
);

SELECT count(*) = 7 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'progression_target_applications_no_delete',
  'progression_document_links_scope_insert',
  'progression_document_links_scope_update',
  'progression_document_links_no_delete',
  'progression_case_arrangements_no_delete',
  'progression_case_offers_target_scope_insert',
  'progression_case_offers_target_scope_update'
);

SELECT count(*) = 15 FROM pragma_table_info('progression_target_applications');
SELECT count(*) = 13 FROM pragma_table_info('progression_document_links');
SELECT count(*) = 10 FROM pragma_table_info('progression_case_arrangements');
