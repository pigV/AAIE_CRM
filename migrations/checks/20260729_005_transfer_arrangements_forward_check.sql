SELECT count(*) = 18
FROM sqlite_master
WHERE type = 'table'
  AND name IN (
      'student_case_sequences',
      'student_cases',
      'student_case_assignments',
      'case_events',
      'student_school_enrolments',
      'transfer_cases',
      'transfer_school_applications',
      'case_record_links',
      'case_material_items',
      'case_payment_requirements',
      'case_arrangement_confirmations',
      'case_completion_confirmations',
      'student_source_tags',
      'external_transfer_case_requests',
      'case_payment_requirement_proofs',
      'transfer_guardianship_arrangements',
      'transfer_accommodation_arrangements',
      'case_arrangement_evidence_links'
  );

SELECT
    (SELECT count(*) FROM pragma_table_info('student_case_sequences')) = 4
AND (SELECT count(*) FROM pragma_table_info('student_cases')) = 17
AND (SELECT count(*) FROM pragma_table_info('student_case_assignments')) = 9
AND (SELECT count(*) FROM pragma_table_info('case_events')) = 14
AND (SELECT count(*) FROM pragma_table_info('student_school_enrolments')) = 18
AND (SELECT count(*) FROM pragma_table_info('transfer_cases')) = 22
AND (SELECT count(*) FROM pragma_table_info('transfer_school_applications')) = 16
AND (SELECT count(*) FROM pragma_table_info('case_record_links')) = 12
AND (SELECT count(*) FROM pragma_table_info('case_material_items')) = 13
AND (SELECT count(*) FROM pragma_table_info('case_payment_requirements')) = 14
AND (SELECT count(*) FROM pragma_table_info('case_arrangement_confirmations')) = 16
AND (SELECT count(*) FROM pragma_table_info('case_completion_confirmations')) = 10
AND (SELECT count(*) FROM pragma_table_info('student_source_tags')) = 8
AND (SELECT count(*) FROM pragma_table_info('external_transfer_case_requests')) = 13
AND (SELECT count(*) FROM pragma_table_info('case_payment_requirement_proofs')) = 10
AND (SELECT count(*) FROM pragma_table_info('transfer_guardianship_arrangements')) = 19
AND (SELECT count(*) FROM pragma_table_info('transfer_accommodation_arrangements')) = 23
AND (SELECT count(*) FROM pragma_table_info('case_arrangement_evidence_links')) = 14;

SELECT count(*) = 19
FROM pragma_table_info('transfer_guardianship_arrangements')
WHERE (name = 'id' AND upper(type) = 'INTEGER' AND pk = 1)
   OR (name = 'case_id' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'version_no' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'arrangement_mode' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'guardian_person_name' AND upper(type) = 'TEXT')
   OR (name = 'guardian_organisation_name' AND upper(type) = 'TEXT')
   OR (name = 'relationship_to_student' AND upper(type) = 'TEXT')
   OR (name = 'provider_name' AND upper(type) = 'TEXT')
   OR (name = 'start_date' AND upper(type) = 'TEXT')
   OR (name = 'end_date' AND upper(type) = 'TEXT')
   OR (name = 'source_type' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'arrangement_status' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'is_current' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'superseded_by' AND upper(type) = 'INTEGER')
   OR (name = 'notes' AND upper(type) = 'TEXT')
   OR (name = 'created_by' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'created_at' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'updated_by' AND upper(type) = 'INTEGER')
   OR (name = 'updated_at' AND upper(type) = 'TEXT' AND "notnull" = 1);

SELECT count(*) = 23
FROM pragma_table_info('transfer_accommodation_arrangements')
WHERE (name = 'id' AND upper(type) = 'INTEGER' AND pk = 1)
   OR (name = 'case_id' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'version_no' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'accommodation_type' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'provider_name' AND upper(type) = 'TEXT')
   OR (name = 'host_name' AND upper(type) = 'TEXT')
   OR (name = 'address_line1' AND upper(type) = 'TEXT')
   OR (name = 'address_line2' AND upper(type) = 'TEXT')
   OR (name = 'suburb' AND upper(type) = 'TEXT')
   OR (name = 'state_code' AND upper(type) = 'TEXT')
   OR (name = 'postcode' AND upper(type) = 'TEXT')
   OR (name = 'country_code' AND upper(type) = 'TEXT')
   OR (name = 'start_date' AND upper(type) = 'TEXT')
   OR (name = 'end_date' AND upper(type) = 'TEXT')
   OR (name = 'source_type' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'arrangement_status' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'is_current' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'superseded_by' AND upper(type) = 'INTEGER')
   OR (name = 'notes' AND upper(type) = 'TEXT')
   OR (name = 'created_by' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'created_at' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'updated_by' AND upper(type) = 'INTEGER')
   OR (name = 'updated_at' AND upper(type) = 'TEXT' AND "notnull" = 1);

SELECT count(*) = 14
FROM pragma_table_info('case_arrangement_evidence_links')
WHERE (name = 'id' AND upper(type) = 'INTEGER' AND pk = 1)
   OR (name = 'case_id' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'arrangement_type' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'arrangement_confirmation_id' AND upper(type) = 'INTEGER')
   OR (name = 'guardianship_arrangement_id' AND upper(type) = 'INTEGER')
   OR (name = 'accommodation_arrangement_id' AND upper(type) = 'INTEGER')
   OR (name = 'case_record_link_id' AND upper(type) = 'INTEGER')
   OR (name = 'document_id' AND upper(type) = 'INTEGER')
   OR (name = 'evidence_role' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'validity_status' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'effective_from' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'superseded_by' AND upper(type) = 'INTEGER')
   OR (name = 'created_by' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'created_at' AND upper(type) = 'TEXT' AND "notnull" = 1);

SELECT count(*) = 5
FROM pragma_table_info('case_arrangement_confirmations')
WHERE (name = 'guardianship_arrangement_id' AND upper(type) = 'INTEGER')
   OR (name = 'accommodation_arrangement_id' AND upper(type) = 'INTEGER')
   OR (name = 'rule_version' AND upper(type) = 'TEXT')
   OR (name = 'requirement_reason' AND upper(type) = 'TEXT')
   OR (name = 'updated_by' AND upper(type) = 'INTEGER');

SELECT count(*) = 5
FROM pragma_foreign_key_list('transfer_guardianship_arrangements')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR ("from" = 'created_by' AND "table" = 'users')
   OR ("from" = 'updated_by' AND "table" = 'users')
   OR (
       "table" = 'transfer_guardianship_arrangements'
       AND "from" IN ('case_id', 'superseded_by')
   );

SELECT count(*) = 5
FROM pragma_foreign_key_list('transfer_accommodation_arrangements')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR ("from" = 'created_by' AND "table" = 'users')
   OR ("from" = 'updated_by' AND "table" = 'users')
   OR (
       "table" = 'transfer_accommodation_arrangements'
       AND "from" IN ('case_id', 'superseded_by')
   );

SELECT count(*) = 8
FROM pragma_foreign_key_list('case_arrangement_evidence_links')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR (
       "from" = 'arrangement_confirmation_id'
       AND "table" = 'case_arrangement_confirmations'
   )
   OR (
       "from" = 'guardianship_arrangement_id'
       AND "table" = 'transfer_guardianship_arrangements'
   )
   OR (
       "from" = 'accommodation_arrangement_id'
       AND "table" = 'transfer_accommodation_arrangements'
   )
   OR ("from" = 'case_record_link_id' AND "table" = 'case_record_links')
   OR ("from" = 'document_id' AND "table" = 'student_documents')
   OR (
       "from" = 'superseded_by'
       AND "table" = 'case_arrangement_evidence_links'
   )
   OR ("from" = 'created_by' AND "table" = 'users');

SELECT count(*) = 5
FROM pragma_foreign_key_list('case_arrangement_confirmations')
WHERE ("from" = 'linked_record_id' AND "table" = 'case_record_links')
   OR ("from" = 'document_id' AND "table" = 'student_documents')
   OR (
       "from" = 'guardianship_arrangement_id'
       AND "table" = 'transfer_guardianship_arrangements'
   )
   OR (
       "from" = 'accommodation_arrangement_id'
       AND "table" = 'transfer_accommodation_arrangements'
   )
   OR ("from" = 'updated_by' AND "table" = 'users');

SELECT count(*) = 7
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_transfer_guardianship_version',
      'uq_transfer_guardianship_current',
      'uq_transfer_accommodation_version',
      'uq_transfer_accommodation_current',
      'uq_arrangement_evidence_current_source',
      'uq_case_record_links_active_case_record',
      'uq_payment_requirement_proofs_current_requirement'
  );

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_transfer_guardianship_current',
      'uq_transfer_accommodation_current'
  )
  AND lower(sql) LIKE '%where is_current = 1%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_arrangement_evidence_current_source'
  AND lower(sql) LIKE '%where validity_status = ''current''%';

SELECT count(*) = 12
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'transfer_guardianship_arrangements_validate_insert',
      'transfer_guardianship_arrangements_validate_update',
      'transfer_guardianship_arrangements_no_delete',
      'transfer_accommodation_arrangements_validate_insert',
      'transfer_accommodation_arrangements_validate_update',
      'transfer_accommodation_arrangements_no_delete',
      'case_arrangement_evidence_links_validate_insert',
      'case_arrangement_evidence_links_validate_update',
      'case_arrangement_evidence_links_no_delete',
      'case_arrangement_confirmations_t7_validate_insert',
      'case_arrangement_confirmations_t7_validate_update',
      'case_record_links_arrangement_confirmation_guard'
  );

SELECT count(*) = 8
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_events_no_update',
      'case_events_no_delete',
      'case_record_links_validate_insert',
      'case_record_links_validate_update',
      'case_record_links_no_delete',
      'case_arrangement_confirmations_no_delete',
      'case_completion_confirmations_no_update',
      'case_completion_confirmations_no_delete'
  );

SELECT count(*) = 3
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'transfer_guardianship_arrangements_no_delete',
      'transfer_accommodation_arrangements_no_delete',
      'case_arrangement_evidence_links_no_delete'
  )
  AND lower(sql) LIKE '%retained permanently%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'transfer_guardianship_arrangements_validate_update',
      'transfer_accommodation_arrangements_validate_update'
  )
  AND lower(sql) LIKE '%identity is immutable%'
  AND lower(sql) LIKE '%history cannot become current%'
  AND lower(sql) LIKE '%superseded link is immutable%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_arrangement_evidence_links_validate_insert'
  AND lower(sql) LIKE '%student_documents%'
  AND lower(sql) LIKE '%student_id%'
  AND lower(sql) LIKE '%case_record_links%'
  AND lower(sql) LIKE '%arrangement_confirmation%'
  AND lower(sql) LIKE '%guardianship%'
  AND lower(sql) LIKE '%accommodation%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_arrangement_confirmations_t7_validate_insert',
      'case_arrangement_confirmations_t7_validate_update'
  )
  AND lower(sql) LIKE '%record_type = new.arrangement_type%'
  AND lower(sql) LIKE '%not applicable confirmation requires a reason%'
  AND lower(sql) LIKE '%transfer_guardianship_arrangements%'
  AND lower(sql) LIKE '%transfer_accommodation_arrangements%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'transfer_guardianship_arrangements'
  AND lower(sql) LIKE '%arrangement_status <> ''confirmed''%'
  AND lower(sql) LIKE '%arrangement_status <> ''not_applicable''%'
  AND lower(sql) LIKE '%superseded_by <> id%'
  AND lower(sql) LIKE '%deferrable initially deferred%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'transfer_accommodation_arrangements'
  AND lower(sql) LIKE '%arrangement_status <> ''confirmed''%'
  AND lower(sql) LIKE '%arrangement_status <> ''not_applicable''%'
  AND lower(sql) LIKE '%state_code not glob%'
  AND lower(sql) LIKE '%country_code not glob%'
  AND lower(sql) LIKE '%deferrable initially deferred%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'case_arrangement_evidence_links'
  AND lower(sql) LIKE '%arrangement_confirmation_id is not null%'
  AND lower(sql) LIKE '%guardianship_arrangement_id is not null%'
  AND lower(sql) LIKE '%accommodation_arrangement_id is not null%'
  AND lower(sql) LIKE '%case_record_link_id is not null or document_id is not null%'
  AND lower(sql) LIKE '%superseded_by <> id%';

SELECT count(*) = 3
FROM schema_migrations
WHERE (version = '20260728_001'
       AND name = 'student_case_phase1'
       AND checksum_sha256 =
           '70f7217b159ff508d7526988cf97d7ab3da2b5e58434a36c75c972a1fefab9e1')
   OR (version = '20260729_002'
       AND name = 'student_case_phase2_transfer'
       AND checksum_sha256 =
           'd7771bffcb2e7cae3aea24dd9582e43f9baaf1a400a664b182b075af0e7bb020')
   OR (version = '20260729_003'
       AND name = 'transfer_case_title'
       AND checksum_sha256 =
           '021836ca3223181acad19a3dea41204b1b22021fb9b1a757e9973c71de0c5cdc');

SELECT count(*) = 1
FROM schema_migrations
WHERE version = '20260729_004'
  AND name = 'transfer_payment_requirement_proofs'
  AND checksum_sha256 =
      'a3b2d049449a5ba63b3e22dc413d39c2d4fee22e99f466f29ef43d6c54c65faa';

SELECT
    count(*) = 0
    OR (
        count(*) = 1
        AND min(name) = 'transfer_arrangements'
        AND min(length(checksum_sha256)) = 64
    )
FROM schema_migrations
WHERE version = '20260729_005';

SELECT
    (SELECT count(*) FROM transfer_guardianship_arrangements) = 0
AND (SELECT count(*) FROM transfer_accommodation_arrangements) = 0
AND (SELECT count(*) FROM case_arrangement_evidence_links) = 0;

SELECT count(*) = 0
FROM pragma_foreign_key_check
WHERE "table" IN (
    'transfer_guardianship_arrangements',
    'transfer_accommodation_arrangements',
    'case_arrangement_evidence_links',
    'case_arrangement_confirmations'
);
