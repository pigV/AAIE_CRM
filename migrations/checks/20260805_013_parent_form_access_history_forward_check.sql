SELECT count(*) = 1 FROM sqlite_master
WHERE type='table' AND name='parent_form_access_events';

SELECT count(*) = 5 FROM pragma_table_info('parent_form_access_events');

SELECT count(*) = 2 FROM sqlite_master
WHERE type='index' AND name IN (
  'idx_parent_form_access_events_token_time',
  'idx_parent_form_access_events_token_type_time'
);

SELECT count(*) = 1 FROM pragma_foreign_key_list('parent_form_access_events')
WHERE [table]='parent_form_tokens' AND [from]='token' AND [to]='token'
  AND on_delete='CASCADE';

SELECT instr(
  (SELECT sql FROM sqlite_master
   WHERE type='table' AND name='parent_form_access_events'),
  "event_type IN ('opened','authenticated')"
) > 0;
