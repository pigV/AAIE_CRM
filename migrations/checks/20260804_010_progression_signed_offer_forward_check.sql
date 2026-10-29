SELECT count(*) = 1 FROM sqlite_master
WHERE type='table' AND name='progression_document_links';

SELECT instr(sql, "'signed_offer'") > 0 FROM sqlite_master
WHERE type='table' AND name='progression_document_links';

SELECT count(*) = 3 FROM sqlite_master
WHERE type='index' AND name IN (
  'ix_progression_document_links_case_role',
  'ix_progression_document_links_target_role',
  'uq_progression_document_links_current'
);

SELECT count(*) = 3 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'progression_document_links_scope_insert',
  'progression_document_links_scope_update',
  'progression_document_links_no_delete'
);

SELECT count(*) = 13 FROM pragma_table_info('progression_document_links');
