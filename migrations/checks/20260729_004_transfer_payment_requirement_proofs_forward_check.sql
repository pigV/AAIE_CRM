SELECT count(*) = 10
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
      'external_transfer_case_requests',
      'case_payment_requirement_proofs'
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
AND (SELECT count(*) FROM pragma_table_info('student_school_enrolments')) = 18
AND (SELECT count(*) FROM pragma_table_info('transfer_cases')) = 22
AND (SELECT count(*) FROM pragma_table_info('transfer_school_applications')) = 16
AND (SELECT count(*) FROM pragma_table_info('case_record_links')) = 12
AND (SELECT count(*) FROM pragma_table_info('case_material_items')) = 13
AND (SELECT count(*) FROM pragma_table_info('case_payment_requirements')) = 14
AND (SELECT count(*) FROM pragma_table_info('case_arrangement_confirmations')) = 11
AND (SELECT count(*) FROM pragma_table_info('case_completion_confirmations')) = 10
AND (SELECT count(*) FROM pragma_table_info('student_source_tags')) = 8
AND (SELECT count(*) FROM pragma_table_info('external_transfer_case_requests')) = 13;

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

SELECT count(*) = 10
FROM pragma_table_info('case_payment_requirement_proofs')
WHERE (name = 'id' AND upper(type) = 'INTEGER' AND pk = 1)
   OR (name = 'case_id' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (
       name = 'transfer_application_id'
       AND upper(type) = 'INTEGER'
       AND "notnull" = 1
   )
   OR (
       name = 'payment_requirement_id'
       AND upper(type) = 'INTEGER'
       AND "notnull" = 1
   )
   OR (
       name = 'case_record_link_id'
       AND upper(type) = 'INTEGER'
       AND "notnull" = 1
   )
   OR (
       name = 'validity_status'
       AND upper(type) = 'TEXT'
       AND "notnull" = 1
   )
   OR (
       name = 'effective_from'
       AND upper(type) = 'TEXT'
       AND "notnull" = 1
   )
   OR (name = 'superseded_by' AND upper(type) = 'INTEGER')
   OR (name = 'created_by' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'created_at' AND upper(type) = 'TEXT' AND "notnull" = 1);

SELECT count(*) = 7
FROM pragma_foreign_key_list('case_payment_requirement_proofs')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR (
       "from" = 'transfer_application_id'
       AND "table" = 'transfer_school_applications'
   )
   OR (
       "from" = 'payment_requirement_id'
       AND "table" = 'case_payment_requirements'
   )
   OR ("from" = 'case_record_link_id' AND "table" = 'case_record_links')
   OR ("from" = 'created_by' AND "table" = 'users')
   OR (
       "table" = 'case_payment_requirement_proofs'
       AND "from" IN ('payment_requirement_id', 'superseded_by')
   );

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'case_payment_requirement_proofs'
  AND lower(sql) LIKE '%validity_status in (''current'', ''superseded'', ''historical'')%'
  AND lower(sql) LIKE '%superseded_by <> id%'
  AND lower(sql) LIKE '%deferrable initially deferred%';

SELECT count(*) = 8
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'ix_payment_requirement_proofs_requirement',
      'ix_payment_requirement_proofs_case_application',
      'ix_payment_requirement_proofs_link',
      'ix_payment_requirement_proofs_status',
      'uq_payment_requirement_proofs_requirement_link',
      'uq_payment_requirement_proofs_link',
      'uq_payment_requirement_proofs_current_requirement',
      'uq_payment_requirement_proofs_current_link'
  );

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_payment_requirement_proofs_current_requirement'
  AND lower(sql) LIKE '%where validity_status = ''current''%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_payment_requirement_proofs_current_link'
  AND lower(sql) LIKE '%where validity_status = ''current''%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_payment_requirement_proofs_requirement_link',
      'uq_payment_requirement_proofs_link'
  )
  AND lower(sql) LIKE '%unique index%';

SELECT count(*) = 4
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_payment_requirement_proofs_validate_insert',
      'case_payment_requirement_proofs_validate_update',
      'case_record_links_payment_proof_relation_guard',
      'case_payment_requirement_proofs_no_delete'
  );

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_payment_requirement_proofs_validate_insert'
  AND lower(sql) LIKE '%case_payment_requirements%'
  AND lower(sql) LIKE '%case_record_links%'
  AND lower(sql) LIKE '%payment_proof%'
  AND lower(sql) LIKE '%student_document%'
  AND lower(sql) LIKE '%payments%'
  AND lower(sql) LIKE '%student_documents%'
  AND lower(sql) LIKE '%student_id%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_payment_requirement_proofs_validate_update'
  AND lower(sql) LIKE '%identity is immutable%'
  AND lower(sql) LIKE '%history cannot become current%'
  AND lower(sql) LIKE '%superseded link cannot be changed%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_payment_requirement_proofs_no_delete'
  AND lower(sql) LIKE '%retained permanently%';

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
WHERE type = 'table'
  AND name = 'transfer_cases'
  AND instr(
      lower(
          replace(
              replace(
                  replace(sql, char(10), ''),
                  char(9),
                  ''
              ),
              ' ',
              ''
          )
      ),
      'titletextcheck(titleisnullor(length(trim(title))>=1andlength(title)<=200))'
  ) > 0;

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

SELECT
    count(*) = 0
    OR (
        count(*) = 1
        AND min(name) = 'transfer_payment_requirement_proofs'
        AND min(length(checksum_sha256)) = 64
    )
FROM schema_migrations
WHERE version = '20260729_004';

SELECT count(*) = 0
FROM case_payment_requirement_proofs;

SELECT count(*) = 0
FROM pragma_foreign_key_check
WHERE "table" = 'case_payment_requirement_proofs';
