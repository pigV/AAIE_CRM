SELECT count(*) = 14
FROM sqlite_master
WHERE type='table' AND name IN (
  'transfer_v2_origin_events','transfer_target_state_events',
  'transfer_document_requirements','transfer_document_bindings',
  'transfer_document_binding_state_events','transfer_document_adoption_events',
  'transfer_payment_aggregates','transfer_payment_events',
  'transfer_coe_confirmations','transfer_coe_events',
  'transfer_v2_lifecycle_events','transfer_v2_command_receipts',
  'transfer_completion_events',
  'transfer_school_library_requests'
);

SELECT count(*) = 9
FROM pragma_table_info('transfer_cases')
WHERE name IN (
  'v2_version','transfer_source_label','current_year_level_snapshot',
  'application_year_level','transfer_year','transfer_term',
  'current_school_state_snapshot','current_school_region_snapshot',
  'current_school_type_snapshot'
);

SELECT count(*) = 7
FROM pragma_table_info('transfer_school_applications')
WHERE name IN (
  'branch_uid','application_cycle_id','original_school_id',
  'school_state_snapshot','school_region_snapshot','school_type_snapshot','updated_by'
);

SELECT count(*) = 1
FROM pragma_table_info('offer_received_events')
WHERE name='transfer_branch_id';

SELECT count(*) = 1
FROM pragma_table_info('cycle_final_acceptances')
WHERE name='transfer_branch_id';

SELECT count(*) = 1
FROM pragma_table_info('student_documents')
WHERE name='document_date';

SELECT count(*) = 1
FROM pragma_table_info('transfer_v2_lifecycle_events')
WHERE name='final_acceptance_id';

SELECT count(*) = 0
FROM transfer_cases
WHERE v2_version IS NOT NULL;

SELECT count(*) = 0
FROM transfer_school_applications
WHERE application_cycle_id IS NOT NULL OR branch_uid IS NOT NULL;

SELECT count(*) = 0 FROM transfer_v2_origin_events;
SELECT count(*) = 0 FROM transfer_target_state_events;
SELECT count(*) = 0 FROM transfer_document_bindings;
SELECT count(*) = 0 FROM transfer_payment_aggregates;
SELECT count(*) = 0 FROM transfer_coe_confirmations;
SELECT count(*) = 0 FROM transfer_v2_command_receipts;
SELECT count(*) = 0 FROM transfer_completion_events;

SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
