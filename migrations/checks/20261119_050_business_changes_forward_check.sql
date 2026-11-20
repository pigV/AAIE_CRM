SELECT
 (SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN
 ('business_change_commands','business_change_events','business_change_versions',
  'business_change_impacts','business_change_deliveries'))=5
 AND (SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN
 ('business_change_events_command_idx','business_change_impacts_subject_idx',
  'business_change_deliveries_due_idx','business_change_deliveries_lease_idx'))=4;
