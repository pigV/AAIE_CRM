SELECT count(*) = 2 FROM sqlite_master
WHERE type='table' AND name IN ('parent_form_access_bindings','parent_form_drafts');

SELECT count(*) = 9 FROM pragma_table_info('parent_form_access_bindings');

SELECT count(*) = 7 FROM pragma_table_info('parent_form_drafts');

SELECT count(*) = 2 FROM sqlite_master
WHERE type='index' AND name IN (
  'idx_parent_form_bindings_openid',
  'idx_parent_form_drafts_saved'
);

SELECT count(*) = 1 FROM pragma_foreign_key_list('parent_form_access_bindings')
WHERE [table]='parent_form_tokens' AND [from]='token' AND [to]='token' AND on_delete='CASCADE';

SELECT count(*) = 1 FROM pragma_foreign_key_list('parent_form_drafts')
WHERE [table]='parent_form_tokens' AND [from]='token' AND [to]='token' AND on_delete='CASCADE';
