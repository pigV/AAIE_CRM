SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='lifecycle_module_disposition_events';

SELECT count(*) = 11
FROM pragma_table_info('lifecycle_module_disposition_events');

SELECT count(*) = 2
FROM pragma_foreign_key_list('lifecycle_module_disposition_events');

SELECT count(*) = 2
FROM sqlite_master
WHERE type='index' AND name IN (
  'idx_lifecycle_module_disposition_current',
  'idx_lifecycle_module_disposition_source'
);

SELECT count(*) = 2
FROM sqlite_master
WHERE type='trigger' AND name IN (
  'lifecycle_module_disposition_events_no_update',
  'lifecycle_module_disposition_events_no_delete'
);

SELECT count(*) = 0
FROM lifecycle_module_disposition_events AS event
LEFT JOIN application_cycles AS cycle
  ON cycle.id=event.application_cycle_id
LEFT JOIN users AS actor
  ON actor.id=event.actor_user_id
WHERE cycle.id IS NULL OR actor.id IS NULL;

SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
