-- Unified Reminder Center canonical identity.
-- Existing IDs are preserved. Historical contract opened/viewed pairs are
-- consolidated in place: the earliest row becomes canonical and every shadow
-- row is retained as Cancelled with an audit note.

ALTER TABLE reminders ADD COLUMN dedupe_key TEXT;
ALTER TABLE reminders ADD COLUMN source_module TEXT;
ALTER TABLE reminders ADD COLUMN source_object_type TEXT;
ALTER TABLE reminders ADD COLUMN source_object_id TEXT;
ALTER TABLE reminders ADD COLUMN source_event_type TEXT;
ALTER TABLE reminders ADD COLUMN source_event_id TEXT;
ALTER TABLE reminders ADD COLUMN opened_at TEXT;
ALTER TABLE reminders ADD COLUMN opened_by INTEGER REFERENCES users(id);
ALTER TABLE reminders ADD COLUMN snoozed_at TEXT;
ALTER TABLE reminders ADD COLUMN snoozed_by INTEGER REFERENCES users(id);
ALTER TABLE reminders ADD COLUMN metadata_json TEXT;
ALTER TABLE reminders ADD COLUMN canonical_version INTEGER NOT NULL DEFAULT 1;

CREATE TEMP TABLE _contract_engagement_groups AS
SELECT
  replace(
    replace(source_record_id, ':opened:', ':parent_engaged:'),
    ':viewed:', ':parent_engaged:'
  ) AS event_key,
  MIN(id) AS keeper_id,
  MIN(CASE WHEN status='Completed' THEN id END) AS completed_source_id,
  MIN(CASE WHEN status='Dismissed' THEN id END) AS dismissed_source_id,
  CASE
    WHEN SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END)>0 THEN 'Completed'
    WHEN SUM(CASE WHEN status='Dismissed' THEN 1 ELSE 0 END)>0 THEN 'Dismissed'
    WHEN SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END)=COUNT(*) THEN 'Cancelled'
    ELSE 'Pending'
  END AS canonical_status
FROM reminders
WHERE source_type='business_event:contract'
  AND reminder_type IN ('contract.opened','contract.viewed')
  AND (
    source_record_id LIKE 'contract:%:opened:%'
    OR source_record_id LIKE 'contract:%:viewed:%'
  )
GROUP BY event_key;

UPDATE reminders
SET
  status=(SELECT canonical_status FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id),
  completed_at=CASE WHEN (SELECT canonical_status FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id)='Completed' THEN COALESCE(completed_at,(
    SELECT source.completed_at FROM _contract_engagement_groups g
    JOIN reminders source ON source.id=g.completed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  completed_by=CASE WHEN (SELECT canonical_status FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id)='Completed' THEN COALESCE(completed_by,(
    SELECT source.completed_by FROM _contract_engagement_groups g
    JOIN reminders source ON source.id=g.completed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  dismissed_at=CASE WHEN (SELECT canonical_status FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id)='Dismissed' THEN COALESCE(dismissed_at,(
    SELECT source.dismissed_at FROM _contract_engagement_groups g
    JOIN reminders source ON source.id=g.dismissed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  dismissed_by=CASE WHEN (SELECT canonical_status FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id)='Dismissed' THEN COALESCE(dismissed_by,(
    SELECT source.dismissed_by FROM _contract_engagement_groups g
    JOIN reminders source ON source.id=g.dismissed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  reminder_type='contract.parent_engaged',
  dedupe_key=(SELECT event_key FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id),
  source_module='contract',
  source_object_type='contract',
  source_object_id=CAST(related_record_id AS TEXT),
  source_event_type='contract.parent_engaged',
  source_event_id=(SELECT event_key FROM _contract_engagement_groups g WHERE g.keeper_id=reminders.id),
  canonical_version=1
WHERE id IN (SELECT keeper_id FROM _contract_engagement_groups);

UPDATE reminders
SET
  status='Cancelled',
  dedupe_key=NULL,
  source_module='contract',
  source_object_type='contract',
  source_object_id=CAST(related_record_id AS TEXT),
  source_event_type='contract.parent_engaged',
  source_event_id=replace(
    replace(source_record_id, ':opened:', ':parent_engaged:'),
    ':viewed:', ':parent_engaged:'
  ),
  notes=trim(
    COALESCE(notes || char(10), '') ||
    'Superseded by canonical contract engagement reminder #' ||
    (SELECT keeper_id FROM _contract_engagement_groups g
      WHERE g.event_key=replace(
        replace(reminders.source_record_id, ':opened:', ':parent_engaged:'),
        ':viewed:', ':parent_engaged:'
      ))
  ),
  canonical_version=1
WHERE source_type='business_event:contract'
  AND reminder_type IN ('contract.opened','contract.viewed')
  AND id NOT IN (SELECT keeper_id FROM _contract_engagement_groups)
  AND replace(
    replace(source_record_id, ':opened:', ':parent_engaged:'),
    ':viewed:', ':parent_engaged:'
  ) IN (SELECT event_key FROM _contract_engagement_groups);

DROP TABLE _contract_engagement_groups;

-- Exact legacy auto-generated duplicates are also consolidated in place. The
-- identity mirrors create_auto_reminder(): source tuple plus schedule, except
-- missing-expiry rules whose identity deliberately omits the schedule.
CREATE TEMP TABLE _auto_duplicate_groups AS
SELECT
  MIN(id) AS keeper_id,
  'auto:' || replace(trim(source_type),':','_') || ':' ||
  replace(trim(source_record_id),':','_') || ':' ||
  replace(trim(reminder_type),':','_') ||
  CASE
    WHEN source_record_id LIKE '%:missing_expiry' OR reminder_type LIKE '%_missing_expiry'
      THEN ''
    ELSE ':' || replace(trim(COALESCE(reminder_datetime,due_datetime,due_date,'')),':','_')
  END AS event_key,
  MIN(CASE WHEN status='Completed' THEN id END) AS completed_source_id,
  MIN(CASE WHEN status='Dismissed' THEN id END) AS dismissed_source_id,
  CASE
    WHEN SUM(CASE WHEN status='Completed' THEN 1 ELSE 0 END)>0 THEN 'Completed'
    WHEN SUM(CASE WHEN status='Dismissed' THEN 1 ELSE 0 END)>0 THEN 'Dismissed'
    WHEN SUM(CASE WHEN status='Cancelled' THEN 1 ELSE 0 END)=COUNT(*) THEN 'Cancelled'
    ELSE 'Pending'
  END AS canonical_status
FROM reminders
WHERE auto_generated=1
  AND COALESCE(source_type,'')<>''
  AND COALESCE(source_record_id,'')<>''
  AND COALESCE(reminder_type,'')<>''
  AND COALESCE(dedupe_key,'')=''
GROUP BY source_type,source_record_id,reminder_type,
  CASE
    WHEN source_record_id LIKE '%:missing_expiry' OR reminder_type LIKE '%_missing_expiry' THEN ''
    ELSE COALESCE(reminder_datetime,due_datetime,due_date,'')
  END
HAVING COUNT(*)>1;

UPDATE reminders
SET
  status=(SELECT canonical_status FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id),
  completed_at=CASE WHEN (SELECT canonical_status FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id)='Completed' THEN COALESCE(completed_at,(
    SELECT source.completed_at FROM _auto_duplicate_groups g
    JOIN reminders source ON source.id=g.completed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  completed_by=CASE WHEN (SELECT canonical_status FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id)='Completed' THEN COALESCE(completed_by,(
    SELECT source.completed_by FROM _auto_duplicate_groups g
    JOIN reminders source ON source.id=g.completed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  dismissed_at=CASE WHEN (SELECT canonical_status FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id)='Dismissed' THEN COALESCE(dismissed_at,(
    SELECT source.dismissed_at FROM _auto_duplicate_groups g
    JOIN reminders source ON source.id=g.dismissed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  dismissed_by=CASE WHEN (SELECT canonical_status FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id)='Dismissed' THEN COALESCE(dismissed_by,(
    SELECT source.dismissed_by FROM _auto_duplicate_groups g
    JOIN reminders source ON source.id=g.dismissed_source_id
    WHERE g.keeper_id=reminders.id
  )) ELSE NULL END,
  dedupe_key=(SELECT event_key FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id),
  source_module=source_type,
  source_object_type=source_type,
  source_object_id=source_record_id,
  source_event_type=reminder_type,
  source_event_id=(SELECT event_key FROM _auto_duplicate_groups g WHERE g.keeper_id=reminders.id),
  canonical_version=1
WHERE id IN (SELECT keeper_id FROM _auto_duplicate_groups);

UPDATE reminders
SET
  status='Cancelled',
  dedupe_key=NULL,
  notes=trim(
    COALESCE(notes || char(10),'') ||
    'Superseded by canonical reminder #' ||
    (SELECT keeper_id FROM _auto_duplicate_groups g
      WHERE g.event_key='auto:' || replace(trim(reminders.source_type),':','_') || ':' ||
        replace(trim(reminders.source_record_id),':','_') || ':' ||
        replace(trim(reminders.reminder_type),':','_') ||
        CASE
          WHEN reminders.source_record_id LIKE '%:missing_expiry' OR reminders.reminder_type LIKE '%_missing_expiry'
            THEN ''
          ELSE ':' || replace(trim(COALESCE(reminders.reminder_datetime,reminders.due_datetime,reminders.due_date,'')),':','_')
        END)
  )
WHERE id NOT IN (SELECT keeper_id FROM _auto_duplicate_groups)
  AND auto_generated=1
  AND COALESCE(dedupe_key,'')=''
  AND 'auto:' || replace(trim(source_type),':','_') || ':' ||
      replace(trim(source_record_id),':','_') || ':' ||
      replace(trim(reminder_type),':','_') ||
      CASE
        WHEN source_record_id LIKE '%:missing_expiry' OR reminder_type LIKE '%_missing_expiry' THEN ''
        ELSE ':' || replace(trim(COALESCE(reminder_datetime,due_datetime,due_date,'')),':','_')
      END IN (SELECT event_key FROM _auto_duplicate_groups);

DROP TABLE _auto_duplicate_groups;

CREATE UNIQUE INDEX uq_reminders_dedupe_key
ON reminders(dedupe_key)
WHERE dedupe_key IS NOT NULL AND dedupe_key<>'';

CREATE INDEX idx_reminders_canonical_source
ON reminders(source_module,source_object_type,source_object_id,source_event_type,source_event_id);
