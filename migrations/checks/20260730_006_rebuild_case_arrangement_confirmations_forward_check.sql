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

SELECT count(*) = 16
FROM pragma_table_info('case_arrangement_confirmations')
WHERE (name = 'id' AND upper(type) = 'INTEGER' AND pk = 1)
   OR (name = 'case_id' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (
       name = 'arrangement_type'
       AND upper(type) = 'TEXT'
       AND "notnull" = 1
   )
   OR (
       name = 'arrangement_status'
       AND upper(type) = 'TEXT'
       AND "notnull" = 1
       AND dflt_value = '''pending'''
   )
   OR (name = 'linked_record_id' AND upper(type) = 'INTEGER')
   OR (name = 'document_id' AND upper(type) = 'INTEGER')
   OR (name = 'notes' AND upper(type) = 'TEXT')
   OR (name = 'confirmed_by' AND upper(type) = 'INTEGER')
   OR (name = 'confirmed_at' AND upper(type) = 'TEXT')
   OR (name = 'created_at' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'updated_at' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (
       name = 'guardianship_arrangement_id'
       AND upper(type) = 'INTEGER'
   )
   OR (
       name = 'accommodation_arrangement_id'
       AND upper(type) = 'INTEGER'
   )
   OR (name = 'rule_version' AND upper(type) = 'TEXT')
   OR (name = 'requirement_reason' AND upper(type) = 'TEXT')
   OR (name = 'updated_by' AND upper(type) = 'INTEGER');

SELECT count(*) = 7
FROM pragma_foreign_key_list('case_arrangement_confirmations')
WHERE ("from" = 'case_id' AND "table" = 'transfer_cases')
   OR ("from" = 'linked_record_id' AND "table" = 'case_record_links')
   OR ("from" = 'document_id' AND "table" = 'student_documents')
   OR ("from" = 'confirmed_by' AND "table" = 'users')
   OR (
       "from" = 'guardianship_arrangement_id'
       AND "table" = 'transfer_guardianship_arrangements'
   )
   OR (
       "from" = 'accommodation_arrangement_id'
       AND "table" = 'transfer_accommodation_arrangements'
   )
   OR ("from" = 'updated_by' AND "table" = 'users');

SELECT count(*) = 1
FROM pragma_index_list('case_arrangement_confirmations')
WHERE "unique" = 1
  AND origin = 'u';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'ix_case_arrangement_confirmations_case_status'
  AND lower(sql) LIKE '%case_id%'
  AND lower(sql) LIKE '%arrangement_status%'
  AND lower(sql) LIKE '%arrangement_type%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'case_arrangement_confirmations'
  AND lower(sql) LIKE '%''visa''%'
  AND lower(sql) LIKE '%''oshc''%'
  AND lower(sql) LIKE '%''guardianship''%'
  AND lower(sql) LIKE '%''accommodation''%'
  AND lower(sql) LIKE '%''transcript_or_school_report''%'
  AND lower(sql) LIKE '%''pending''%'
  AND lower(sql) LIKE '%''carried_forward''%'
  AND lower(sql) LIKE '%''updated''%'
  AND lower(sql) LIKE '%''confirmed''%'
  AND lower(sql) LIKE '%''not_applicable''%'
  AND lower(sql) LIKE '%confirmed_by is not null%'
  AND lower(sql) LIKE '%confirmed_at is not null%'
  AND lower(sql) LIKE '%not_applicable%'
  AND lower(sql) LIKE '%requirement_reason%'
  AND lower(sql) LIKE '%guardianship_arrangement_id is not null%'
  AND lower(sql) LIKE '%accommodation_arrangement_id is not null%'
  AND lower(sql) LIKE '%linked_record_id is not null%';

SELECT count(*) = 5
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_arrangement_confirmations_validate_insert',
      'case_arrangement_confirmations_validate_update',
      'case_arrangement_confirmations_no_delete',
      'case_arrangement_confirmations_t7_validate_insert',
      'case_arrangement_confirmations_t7_validate_update'
  );

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_arrangement_confirmations_validate_insert',
      'case_arrangement_confirmations_validate_update'
  )
  AND lower(sql) LIKE '%case_record_links%'
  AND lower(sql) LIKE '%student_documents%'
  AND lower(sql) LIKE '%student_cases%'
  AND lower(sql) LIKE '%another case or student%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_arrangement_confirmations_t7_validate_insert',
      'case_arrangement_confirmations_t7_validate_update'
  )
  AND lower(sql) LIKE '%record_type = new.arrangement_type%'
  AND lower(sql) LIKE '%transfer_guardianship_arrangements%'
  AND lower(sql) LIKE '%transfer_accommodation_arrangements%'
  AND lower(sql) LIKE '%arrangement.is_current = 1%'
  AND lower(sql) LIKE '%arrangement.arrangement_status%'
  AND lower(sql) LIKE '%not_applicable%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_arrangement_confirmations_t7_validate_update'
  AND lower(sql) LIKE '%new.case_id is not old.case_id%'
  AND lower(sql) LIKE '%new.arrangement_type is not old.arrangement_type%'
  AND lower(sql) LIKE '%identity is immutable%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_arrangement_confirmations_no_delete'
  AND lower(sql) LIKE '%before delete%'
  AND lower(sql) LIKE '%retained permanently%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_record_links_arrangement_confirmation_guard'
  AND lower(sql) LIKE '%arrangement_confirmation%'
  AND lower(sql) LIKE '%record_type%'
  AND lower(sql) LIKE '%validity_status%';

SELECT count(*) = 11
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
      'case_events_no_update',
      'case_events_no_delete'
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

SELECT count(*) = 0
FROM case_arrangement_evidence_links AS evidence
LEFT JOIN case_arrangement_confirmations AS confirmation
  ON confirmation.id = evidence.arrangement_confirmation_id
WHERE evidence.arrangement_confirmation_id IS NOT NULL
  AND (
      confirmation.id IS NULL
      OR confirmation.case_id <> evidence.case_id
      OR confirmation.arrangement_type <> evidence.arrangement_type
  );

SELECT count(*) = 0
FROM case_arrangement_confirmations AS confirmation
LEFT JOIN case_record_links AS link
  ON link.id = confirmation.linked_record_id
WHERE confirmation.arrangement_type IN ('visa', 'oshc')
  AND confirmation.linked_record_id IS NOT NULL
  AND (
      link.id IS NULL
      OR link.case_id <> confirmation.case_id
      OR link.record_type <> confirmation.arrangement_type
      OR (
          confirmation.arrangement_status = 'carried_forward'
          AND link.validity_status <> 'carried_forward'
      )
      OR (
          confirmation.arrangement_status IN ('updated', 'confirmed')
          AND link.validity_status NOT IN ('current', 'carried_forward')
      )
  );

SELECT count(*) = 0
FROM case_arrangement_confirmations
WHERE (
    arrangement_type IN ('visa', 'oshc')
    AND (
        guardianship_arrangement_id IS NOT NULL
        OR accommodation_arrangement_id IS NOT NULL
        OR (
            arrangement_status IN (
                'carried_forward',
                'updated',
                'confirmed'
            )
            AND linked_record_id IS NULL
        )
    )
)
OR (
    arrangement_type = 'guardianship'
    AND (
        linked_record_id IS NOT NULL
        OR accommodation_arrangement_id IS NOT NULL
        OR (
            arrangement_status IN (
                'carried_forward',
                'updated',
                'confirmed'
            )
            AND guardianship_arrangement_id IS NULL
        )
    )
)
OR (
    arrangement_type = 'accommodation'
    AND (
        linked_record_id IS NOT NULL
        OR guardianship_arrangement_id IS NOT NULL
        OR (
            arrangement_status IN (
                'carried_forward',
                'updated',
                'confirmed'
            )
            AND accommodation_arrangement_id IS NULL
        )
    )
)
OR (
    arrangement_type = 'transcript_or_school_report'
    AND (
        guardianship_arrangement_id IS NOT NULL
        OR accommodation_arrangement_id IS NOT NULL
        OR (
            arrangement_status IN ('carried_forward', 'updated')
            AND linked_record_id IS NULL
            AND document_id IS NULL
        )
    )
)
OR (
    arrangement_status = 'confirmed'
    AND (confirmed_by IS NULL OR confirmed_at IS NULL)
)
OR (
    arrangement_status = 'not_applicable'
    AND length(trim(ifnull(requirement_reason, ''))) = 0
);

SELECT count(*) = 0
FROM case_arrangement_confirmations AS confirmation
LEFT JOIN transfer_guardianship_arrangements AS arrangement
  ON arrangement.id = confirmation.guardianship_arrangement_id
WHERE confirmation.guardianship_arrangement_id IS NOT NULL
  AND (
      confirmation.arrangement_type <> 'guardianship'
      OR arrangement.id IS NULL
      OR arrangement.case_id <> confirmation.case_id
      OR arrangement.is_current <> 1
      OR arrangement.arrangement_status <> confirmation.arrangement_status
  );

SELECT count(*) = 0
FROM case_arrangement_confirmations AS confirmation
LEFT JOIN transfer_accommodation_arrangements AS arrangement
  ON arrangement.id = confirmation.accommodation_arrangement_id
WHERE confirmation.accommodation_arrangement_id IS NOT NULL
  AND (
      confirmation.arrangement_type <> 'accommodation'
      OR arrangement.id IS NULL
      OR arrangement.case_id <> confirmation.case_id
      OR arrangement.is_current <> 1
      OR arrangement.arrangement_status <> confirmation.arrangement_status
  );

SELECT count(*) = 5
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
           '021836ca3223181acad19a3dea41204b1b22021fb9b1a757e9973c71de0c5cdc')
   OR (version = '20260729_004'
       AND name = 'transfer_payment_requirement_proofs'
       AND checksum_sha256 =
           'a3b2d049449a5ba63b3e22dc413d39c2d4fee22e99f466f29ef43d6c54c65faa')
   OR (version = '20260729_005'
       AND name = 'transfer_arrangements'
       AND checksum_sha256 =
           '17f7913e12598586df7f241c19849d2c0fe891d129066c6df03b01877b65a469');

SELECT count(*) <= 1
   AND count(*) = count(
       CASE
           WHEN name = 'rebuild_case_arrangement_confirmations'
            AND length(checksum_sha256) = 64
           THEN 1
       END
   )
FROM schema_migrations
WHERE version = '20260730_006';

SELECT count(*) = 0
FROM sqlite_master
WHERE name IN (
    'case_arrangement_confirmations_rebuild_006',
    'migration_006_confirmation_copy_guard',
    'migration_006_confirmation_final_guard'
);

SELECT count(*) = 0
FROM pragma_foreign_key_check
WHERE "table" IN (
    'case_arrangement_confirmations',
    'case_arrangement_evidence_links',
    'case_record_links',
    'transfer_guardianship_arrangements',
    'transfer_accommodation_arrangements'
);
