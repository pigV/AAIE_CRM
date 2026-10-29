SELECT count(*) = 25
FROM pragma_table_info('transfer_cases');

SELECT count(*) = 3
FROM pragma_table_info('transfer_cases')
WHERE (
        name = 'current_school_enrolment_id'
        AND upper(type) = 'INTEGER'
        AND "notnull" = 0
        AND dflt_value IS NULL
      )
   OR (
        name = 'upcoming_school_enrolment_id'
        AND upper(type) = 'INTEGER'
        AND "notnull" = 0
        AND dflt_value IS NULL
      )
   OR (
        name = 'relationship_status'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
        AND dflt_value = '''needs_repair'''
      );

SELECT count(*) = 2
FROM pragma_foreign_key_list('transfer_cases')
WHERE ("from" = 'current_school_enrolment_id'
       AND "table" = 'student_school_enrolments'
       AND "on_delete" = 'RESTRICT')
   OR ("from" = 'upcoming_school_enrolment_id'
       AND "table" = 'student_school_enrolments'
       AND "on_delete" = 'RESTRICT');

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'transfer_cases'
  AND lower(sql) LIKE '%''needs_repair''%'
  AND lower(sql) LIKE '%''current_ready''%'
  AND lower(sql) LIKE '%''ready''%'
  AND lower(sql) LIKE '%''transitioned''%'
  AND lower(sql) LIKE '%''reverted''%';

SELECT count(*) = 26
FROM pragma_table_info('case_completion_confirmations');

SELECT count(*) = 26
FROM pragma_table_info('case_completion_confirmations')
WHERE (name = 'id' AND upper(type) = 'INTEGER' AND pk = 1)
   OR (name = 'case_id' AND upper(type) = 'INTEGER' AND "notnull" = 1)
   OR (name = 'action' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'reverses_confirmation_id' AND upper(type) = 'INTEGER')
   OR (
        name = 'actor_user_id'
        AND upper(type) = 'INTEGER'
        AND "notnull" = 1
      )
   OR (name = 'confirmed_at' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'reason' AND upper(type) = 'TEXT')
   OR (name = 'effective_date' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'idempotency_key' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (
        name = 'workflow_state_before'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'workflow_state_after'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'case_status_before'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'case_status_after'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'active_slot_before'
        AND upper(type) = 'INTEGER'
        AND "notnull" = 1
      )
   OR (
        name = 'active_slot_after'
        AND upper(type) = 'INTEGER'
        AND "notnull" = 1
      )
   OR (name = 'terminal_result_before' AND upper(type) = 'TEXT')
   OR (name = 'terminal_result_after' AND upper(type) = 'TEXT')
   OR (
        name = 'current_enrolment_before_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'current_enrolment_after_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'upcoming_enrolment_before_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'upcoming_enrolment_after_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'readiness_snapshot_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'school_state_before_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (
        name = 'school_state_after_json'
        AND upper(type) = 'TEXT'
        AND "notnull" = 1
      )
   OR (name = 'metadata_json' AND upper(type) = 'TEXT' AND "notnull" = 1)
   OR (name = 'created_at' AND upper(type) = 'TEXT' AND "notnull" = 1);

SELECT count(*) = 3
FROM pragma_foreign_key_list('case_completion_confirmations')
WHERE ("from" = 'case_id'
       AND "table" = 'transfer_cases'
       AND "on_delete" = 'RESTRICT')
   OR ("from" = 'reverses_confirmation_id'
       AND "table" = 'case_completion_confirmations'
       AND "on_delete" = 'RESTRICT')
   OR ("from" = 'actor_user_id'
       AND "table" = 'users'
       AND "on_delete" = 'RESTRICT');

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'table'
  AND name = 'case_completion_confirmations'
  AND lower(sql) LIKE '%''complete''%'
  AND lower(sql) LIKE '%''undo_completion''%'
  AND lower(sql) NOT LIKE '%''undo_complete''%'
  AND lower(sql) LIKE '%json_valid(current_enrolment_before_json)%'
  AND lower(sql) LIKE '%json_valid(current_enrolment_after_json)%'
  AND lower(sql) LIKE '%json_valid(upcoming_enrolment_before_json)%'
  AND lower(sql) LIKE '%json_valid(upcoming_enrolment_after_json)%'
  AND lower(sql) LIKE '%json_valid(readiness_snapshot_json)%'
  AND lower(sql) LIKE '%json_valid(school_state_before_json)%'
  AND lower(sql) LIKE '%json_valid(school_state_after_json)%'
  AND lower(sql) LIKE '%json_valid(metadata_json)%';

SELECT count(*) = 7
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_case_completion_idempotency',
      'uq_case_completion_one_undo_per_completion',
      'ix_case_completion_confirmations_case',
      'ix_case_completion_confirmations_confirmed_at',
      'ix_transfer_cases_current_enrolment',
      'ix_transfer_cases_upcoming_enrolment',
      'ix_transfer_cases_relationship_status'
  );

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_case_completion_idempotency',
      'uq_case_completion_one_undo_per_completion'
  )
  AND lower(sql) LIKE '%unique%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_case_completion_one_undo_per_completion'
  AND lower(sql) LIKE '%where reverses_confirmation_id is not null%';

SELECT count(*) = 6
FROM sqlite_master
WHERE type = 'trigger'
  AND name IN (
      'case_completion_confirmations_validate_reverse',
      'case_completion_confirmations_no_update',
      'case_completion_confirmations_no_delete',
      'transfer_cases_relationship_validate_insert',
      'transfer_cases_relationship_validate_update',
      'student_school_enrolments_transfer_reference_guard'
  );

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'case_completion_confirmations_validate_reverse'
  AND lower(sql) LIKE '%completed.action = ''complete''%'
  AND lower(sql) LIKE '%completed.case_id = new.case_id%'
  AND lower(sql) LIKE '%restore the reversed completion state%';

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
      'transfer_cases_relationship_validate_insert',
      'transfer_cases_relationship_validate_update'
  )
  AND lower(sql) LIKE '%student_school_enrolments%'
  AND lower(sql) LIKE '%parent_case.student_id%'
  AND lower(sql) LIKE '%enrolment.source_case_id = new.case_id%'
  AND lower(sql) LIKE '%current and upcoming enrolments must differ%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'trigger'
  AND name = 'student_school_enrolments_transfer_reference_guard'
  AND lower(sql) LIKE '%transfer.current_school_enrolment_id = old.id%'
  AND lower(sql) LIKE '%transfer.upcoming_school_enrolment_id = old.id%'
  AND lower(sql) LIKE '%cannot invalidate transfer relationship%';

SELECT count(*) = 0
FROM transfer_cases
WHERE relationship_status NOT IN (
          'needs_repair',
          'current_ready',
          'ready',
          'transitioned',
          'reverted'
      )
   OR (
        relationship_status = 'needs_repair'
        AND (
            current_school_enrolment_id IS NOT NULL
            OR upcoming_school_enrolment_id IS NOT NULL
        )
      )
   OR (
        relationship_status IN ('current_ready', 'transitioned', 'reverted')
        AND (
            current_school_enrolment_id IS NULL
            OR upcoming_school_enrolment_id IS NOT NULL
        )
      )
   OR (
        relationship_status = 'ready'
        AND (
            current_school_enrolment_id IS NULL
            OR upcoming_school_enrolment_id IS NULL
        )
      )
   OR (
        current_school_enrolment_id IS NOT NULL
        AND current_school_enrolment_id IS upcoming_school_enrolment_id
      );

SELECT count(*) = 0
FROM transfer_cases AS transfer
JOIN student_cases AS parent_case
  ON parent_case.id = transfer.case_id
LEFT JOIN student_school_enrolments AS current_enrolment
  ON current_enrolment.id = transfer.current_school_enrolment_id
WHERE transfer.current_school_enrolment_id IS NOT NULL
  AND (
      current_enrolment.id IS NULL
      OR current_enrolment.student_id <> parent_case.student_id
      OR current_enrolment.enrolment_role <> 'current'
      OR current_enrolment.status <> 'active'
  );

SELECT count(*) = 0
FROM transfer_cases AS transfer
JOIN student_cases AS parent_case
  ON parent_case.id = transfer.case_id
LEFT JOIN student_school_enrolments AS upcoming_enrolment
  ON upcoming_enrolment.id = transfer.upcoming_school_enrolment_id
WHERE transfer.upcoming_school_enrolment_id IS NOT NULL
  AND (
      upcoming_enrolment.id IS NULL
      OR upcoming_enrolment.student_id <> parent_case.student_id
      OR upcoming_enrolment.enrolment_role <> 'upcoming'
      OR upcoming_enrolment.status <> 'active'
      OR upcoming_enrolment.source_case_id <> transfer.case_id
  );

SELECT count(*) = 0
FROM case_completion_confirmations AS confirmation
LEFT JOIN case_completion_confirmations AS reversed
  ON reversed.id = confirmation.reverses_confirmation_id
WHERE (
        confirmation.action = 'complete'
        AND confirmation.reverses_confirmation_id IS NOT NULL
      )
   OR (
        confirmation.action = 'undo_completion'
        AND (
            reversed.id IS NULL
            OR reversed.action <> 'complete'
            OR reversed.case_id <> confirmation.case_id
        )
      );

SELECT count(*) = 0
FROM sqlite_master
WHERE name IN (
    'case_completion_confirmations_rebuild_007',
    'migration_007_completion_empty_guard',
    'migration_007_completion_empty_guard_reject'
);

SELECT count(*) <= 1
   AND count(*) = count(
       CASE
           WHEN name = 'transfer_completion_contract'
            AND length(checksum_sha256) = 64
           THEN 1
       END
   )
FROM schema_migrations
WHERE version = '20260730_007';

SELECT count(*) = 0
FROM pragma_foreign_key_check
WHERE "table" IN (
    'transfer_cases',
    'case_completion_confirmations',
    'student_school_enrolments',
    'student_cases'
);

SELECT lower(quick_check) = 'ok'
FROM pragma_quick_check;

SELECT lower(integrity_check) = 'ok'
FROM pragma_integrity_check;
