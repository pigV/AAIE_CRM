SELECT
 (SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN
 ('business_change_commands','business_change_events','business_change_versions',
  'business_change_impacts','business_change_deliveries'))=5
 AND (SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN
 ('business_change_events_command_idx','business_change_impacts_subject_idx',
  'business_change_deliveries_due_idx','business_change_deliveries_lease_idx'))=4
 AND (SELECT count(*) FROM sqlite_master
      WHERE type='index' AND name='idx_audit_module_action' AND tbl_name='audit_logs')=1
 AND (SELECT count(*) FROM pragma_index_list('audit_logs')
      WHERE name='idx_audit_module_action' AND "unique"=0 AND partial=0)=1
 AND (SELECT count(*) FROM pragma_index_info('idx_audit_module_action'))=2
 AND (SELECT count(*) FROM pragma_index_info('idx_audit_module_action')
      WHERE seqno=0 AND name='module_name')=1
 AND (SELECT count(*) FROM pragma_index_info('idx_audit_module_action')
      WHERE seqno=1 AND name='action_type')=1;
