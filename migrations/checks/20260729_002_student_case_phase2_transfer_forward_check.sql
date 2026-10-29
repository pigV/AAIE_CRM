SELECT count(*) = 9
FROM sqlite_master
WHERE type = 'table'
  AND name IN (
      'transfer_cases',
      'transfer_school_applications',
      'case_record_links',
      'case_material_items',
      'case_payment_requirements',
      'case_arrangement_confirmations',
      'case_completion_confirmations',
      'student_source_tags',
      'external_transfer_case_requests'
  );

SELECT count(*) = 5
FROM sqlite_master
WHERE type = 'table'
  AND name IN (
      'student_case_sequences',
      'student_cases',
      'student_case_assignments',
      'case_events',
      'student_school_enrolments'
  );

SELECT
    (SELECT count(*) FROM pragma_table_info('student_case_sequences')) = 4
AND (SELECT count(*) FROM pragma_table_info('student_cases')) = 17
AND (SELECT count(*) FROM pragma_table_info('student_case_assignments')) = 9
AND (SELECT count(*) FROM pragma_table_info('case_events')) = 14
AND (SELECT count(*) FROM pragma_table_info('student_school_enrolments')) = 18;

SELECT count(*) = 8
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'student_case_sequences_no_delete',
      'student_cases_no_delete',
      'student_cases_archive_from_terminal_only',
      'student_case_assignments_no_delete',
      'student_case_assignments_identity_immutable',
      'case_events_no_update',
      'case_events_no_delete',
      'student_school_enrolments_no_delete'
  );

SELECT count(*) = 5
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_student_cases_one_active_per_student',
      'uq_student_case_assignments_current_role_user',
      'uq_student_case_assignments_one_current_owner',
      'uq_student_school_enrolments_one_current',
      'uq_student_school_enrolments_one_upcoming'
  );

SELECT count(*) = 0
FROM sqlite_master
WHERE type = 'table'
  AND name IN (
      'progression_cases',
      'progression_plans',
      'progression_plan_stages',
      'progression_material_items'
  );

SELECT count(*) = 21
FROM pragma_table_info('transfer_cases');

SELECT count(*) = 16
FROM pragma_table_info('transfer_school_applications');

SELECT count(*) = 12
FROM pragma_table_info('case_record_links');

SELECT count(*) = 13
FROM pragma_table_info('case_material_items');

SELECT count(*) = 14
FROM pragma_table_info('case_payment_requirements');

SELECT count(*) = 11
FROM pragma_table_info('case_arrangement_confirmations');

SELECT count(*) = 10
FROM pragma_table_info('case_completion_confirmations');

SELECT count(*) = 8
FROM pragma_table_info('student_source_tags');

SELECT count(*) = 13
FROM pragma_table_info('external_transfer_case_requests');

SELECT count(*) = 21
FROM pragma_table_info('transfer_cases')
WHERE (name = 'case_id' AND upper(type) = 'INTEGER')
   OR (name = 'transfer_origin' AND upper(type) = 'TEXT')
   OR (name = 'source_contract_id' AND upper(type) = 'INTEGER')
   OR (name = 'workflow_state' AND upper(type) = 'TEXT')
   OR (name = 'current_school_name_snapshot' AND upper(type) = 'TEXT')
   OR (name = 'current_school_id' AND upper(type) = 'INTEGER')
   OR (name = 'current_city_key' AND upper(type) = 'TEXT')
   OR (name = 'current_school_sector' AND upper(type) = 'TEXT')
   OR (name = 'target_city_key' AND upper(type) = 'TEXT')
   OR (name = 'target_school_sector' AND upper(type) = 'TEXT')
   OR (name = 'rule_status' AND upper(type) = 'TEXT')
   OR (name = 'same_city_result' AND upper(type) = 'TEXT')
   OR (name = 'sector_transition' AND upper(type) = 'TEXT')
   OR (name = 'required_completion_document' AND upper(type) = 'TEXT')
   OR (name = 'tuition_requirement' AND upper(type) = 'TEXT')
   OR (name = 'final_application_id' AND upper(type) = 'INTEGER')
   OR (name = 'rule_calculated_at' AND upper(type) = 'TEXT')
   OR (name = 'rule_calculated_by' AND upper(type) = 'INTEGER')
   OR (name = 'rule_version' AND upper(type) = 'TEXT')
   OR (name = 'created_at' AND upper(type) = 'TEXT')
   OR (name = 'updated_at' AND upper(type) = 'TEXT');

SELECT count(*) = 16
FROM pragma_table_info('transfer_school_applications')
WHERE (name = 'id' AND upper(type) = 'INTEGER')
   OR (name = 'case_id' AND upper(type) = 'INTEGER')
   OR (name = 'school_id' AND upper(type) = 'INTEGER')
   OR (name = 'school_name_snapshot' AND upper(type) = 'TEXT')
   OR (name = 'institution_group_id' AND upper(type) = 'INTEGER')
   OR (name = 'city_key' AND upper(type) = 'TEXT')
   OR (name = 'campus_key' AND upper(type) = 'TEXT')
   OR (name = 'school_sector' AND upper(type) = 'TEXT')
   OR (name = 'expected_start_date' AND upper(type) = 'TEXT')
   OR (name = 'preference_order' AND upper(type) = 'INTEGER')
   OR (name = 'application_status' AND upper(type) = 'TEXT')
   OR (name = 'notes' AND upper(type) = 'TEXT')
   OR (name = 'is_final' AND upper(type) = 'INTEGER')
   OR (name = 'created_by' AND upper(type) = 'INTEGER')
   OR (name = 'created_at' AND upper(type) = 'TEXT')
   OR (name = 'updated_at' AND upper(type) = 'TEXT');

SELECT count(*) = 12
FROM pragma_table_info('case_record_links')
WHERE (name = 'id' AND upper(type) = 'INTEGER')
   OR (name = 'case_id' AND upper(type) = 'INTEGER')
   OR (name = 'transfer_application_id' AND upper(type) = 'INTEGER')
   OR (name = 'record_type' AND upper(type) = 'TEXT')
   OR (name = 'record_id' AND upper(type) = 'INTEGER')
   OR (name = 'document_id' AND upper(type) = 'INTEGER')
   OR (name = 'link_role' AND upper(type) = 'TEXT')
   OR (name = 'validity_status' AND upper(type) = 'TEXT')
   OR (name = 'effective_from' AND upper(type) = 'TEXT')
   OR (name = 'superseded_by' AND upper(type) = 'INTEGER')
   OR (name = 'created_by' AND upper(type) = 'INTEGER')
   OR (name = 'created_at' AND upper(type) = 'TEXT');

SELECT count(*) = 8
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_transfer_school_applications_one_final',
      'uq_transfer_school_applications_active_preference',
      'uq_case_record_links_same_record_context',
      'uq_case_record_links_current_application_record',
      'uq_case_record_links_active_case_record',
      'uq_student_source_tags_current',
      'uq_external_transfer_requests_idempotency',
      'uq_external_transfer_requests_contract_student'
  );

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_transfer_school_applications_one_final'
  AND lower(sql) LIKE '%where is_final = 1%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_case_record_links_current_application_record'
  AND lower(sql) LIKE '%validity_status = ''current''%'
  AND lower(sql) LIKE '%''offer''%'
  AND lower(sql) LIKE '%''coe''%'
  AND lower(sql) LIKE '%''confirmation_letter''%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_case_record_links_active_case_record'
  AND lower(sql) LIKE '%''visa''%'
  AND lower(sql) LIKE '%''oshc''%'
  AND lower(sql) LIKE '%''carried_forward''%';

SELECT count(*) = 9
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'transfer_cases_no_delete',
      'transfer_school_applications_no_delete',
      'case_record_links_no_delete',
      'case_material_items_no_delete',
      'case_payment_requirements_no_delete',
      'case_arrangement_confirmations_no_delete',
      'case_completion_confirmations_no_delete',
      'student_source_tags_no_delete',
      'external_transfer_case_requests_no_delete'
  );

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_completion_confirmations_no_update',
      'case_completion_confirmations_no_delete'
  )
  AND lower(sql) LIKE '%append-only%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_record_links_validate_insert',
      'case_record_links_validate_update'
  )
  AND lower(sql) LIKE '%offers%'
  AND lower(sql) LIKE '%coe_oshc%'
  AND lower(sql) LIKE '%visas%'
  AND lower(sql) LIKE '%payments%'
  AND lower(sql) LIKE '%student_documents%'
  AND lower(sql) LIKE '%student_id%';

SELECT count(*) = 4
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'transfer_cases_final_application_insert_guard',
      'transfer_cases_final_application_update_guard',
      'transfer_school_applications_sync_final_insert',
      'transfer_school_applications_sync_final_update'
  );

SELECT count(*) >= 3
FROM pragma_foreign_key_list('transfer_cases')
WHERE ("from" = 'case_id' AND "table" = 'student_cases')
   OR ("from" = 'current_school_id' AND "table" = 'schools')
   OR ("from" = 'rule_calculated_by' AND "table" = 'users')
   OR ("from" = 'source_contract_id' AND "table" = 'contracts')
   OR ("from" = 'final_application_id'
       AND "table" = 'transfer_school_applications');

SELECT count(*) = 3
FROM pragma_foreign_key_list('transfer_school_applications')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR ("from" = 'school_id' AND "table" = 'schools')
   OR ("from" = 'created_by' AND "table" = 'users');

SELECT count(*) >= 5
FROM pragma_foreign_key_list('case_record_links')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR ("from" = 'transfer_application_id'
       AND "table" = 'transfer_school_applications')
   OR ("from" = 'document_id' AND "table" = 'student_documents')
   OR ("from" = 'superseded_by' AND "table" = 'case_record_links')
   OR ("from" = 'created_by' AND "table" = 'users');

SELECT count(*) = 0
FROM pragma_foreign_key_check
WHERE "table" IN (
    'transfer_cases',
    'transfer_school_applications',
    'case_record_links',
    'case_material_items',
    'case_payment_requirements',
    'case_arrangement_confirmations',
    'case_completion_confirmations',
    'student_source_tags',
    'external_transfer_case_requests'
);

SELECT (
    SELECT count(*) FROM transfer_cases
) + (
    SELECT count(*) FROM transfer_school_applications
) + (
    SELECT count(*) FROM case_record_links
) + (
    SELECT count(*) FROM case_material_items
) + (
    SELECT count(*) FROM case_payment_requirements
) + (
    SELECT count(*) FROM case_arrangement_confirmations
) + (
    SELECT count(*) FROM case_completion_confirmations
) + (
    SELECT count(*) FROM student_source_tags
) + (
    SELECT count(*) FROM external_transfer_case_requests
) = 0;

SELECT
    count(*) = 0
    OR (
        count(*) = 1
        AND min(name) = 'student_case_phase2_transfer'
        AND min(length(checksum_sha256)) = 64
    )
FROM schema_migrations
WHERE version = '20260729_002';
