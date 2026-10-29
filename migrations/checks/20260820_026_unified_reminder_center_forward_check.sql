SELECT count(*) = 12 FROM pragma_table_info('reminders')
WHERE name IN (
  'dedupe_key','source_module','source_object_type','source_object_id',
  'source_event_type','source_event_id','opened_at','opened_by','snoozed_at','snoozed_by',
  'metadata_json','canonical_version'
);

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_reminders_dedupe_key' AND sql LIKE '%UNIQUE%dedupe_key%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='idx_reminders_canonical_source';

SELECT count(*) = 0 FROM reminders WHERE canonical_version IS NULL;

SELECT count(*) = 0
FROM (
  SELECT dedupe_key
  FROM reminders
  WHERE dedupe_key IS NOT NULL AND dedupe_key<>''
  GROUP BY dedupe_key
  HAVING count(*)>1
);

SELECT count(*) = 0
FROM reminders
WHERE dedupe_key IS NOT NULL AND dedupe_key<>''
  AND (
    (status='Completed' AND (dismissed_at IS NOT NULL OR dismissed_by IS NOT NULL))
    OR (status='Dismissed' AND (completed_at IS NOT NULL OR completed_by IS NOT NULL))
  );

SELECT count(*) = 0
FROM reminders
WHERE source_type='business_event:contract'
  AND reminder_type IN ('contract.opened','contract.viewed')
  AND status NOT IN ('Cancelled','Completed','Dismissed');

SELECT count(*) = 0
FROM (
  SELECT source_type,source_record_id,reminder_type,
    CASE
      WHEN source_record_id LIKE '%:missing_expiry' OR reminder_type LIKE '%_missing_expiry' THEN ''
      ELSE COALESCE(reminder_datetime,due_datetime,due_date,'')
    END AS schedule_key
  FROM reminders
  WHERE auto_generated=1
    AND status NOT IN ('Cancelled','Completed','Dismissed')
  GROUP BY source_type,source_record_id,reminder_type,schedule_key
  HAVING count(*)>1
);
