SELECT CASE WHEN
  EXISTS(SELECT 1 FROM pragma_table_info('visa_result_status_events') WHERE name='effective_result') AND
  EXISTS(SELECT 1 FROM pragma_table_info('visa_result_status_current') WHERE name='manual_override') AND
  (SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN (
    'visa_result_status_events_no_update',
    'visa_result_status_events_no_delete',
    'visa_result_status_events_document_authority'
  ))=3
THEN 1 ELSE 0 END AS ok;
