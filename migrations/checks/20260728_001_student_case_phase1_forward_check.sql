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

SELECT count(*) = 0
FROM sqlite_master
WHERE type = 'table'
  AND name IN (
      'institution_groups',
      'transfer_cases',
      'transfer_school_applications',
      'progression_cases',
      'progression_plans',
      'progression_plan_stages',
      'case_record_links',
      'case_material_items',
      'case_payment_requirements',
      'case_arrangement_confirmations',
      'case_completion_confirmations'
  );

SELECT
    count(*) = 4
    AND sum(
        CASE
            WHEN name = 'student_id' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'case_type' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'next_sequence_no' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'updated_at' AND upper(type) = 'TEXT' THEN 1
            ELSE 0
        END
    ) = 4
FROM pragma_table_info('student_case_sequences');

SELECT
    count(*) = 17
    AND sum(
        CASE
            WHEN name = 'id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'student_id' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'case_type' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'sequence_no' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'status' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'terminal_result' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'active_slot' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'started_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'paused_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'completed_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'cancelled_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'failed_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'archived_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'created_by' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'created_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'updated_by' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'updated_at' AND upper(type) = 'TEXT' THEN 1
            ELSE 0
        END
    ) = 17
FROM pragma_table_info('student_cases');

SELECT
    count(*) = 9
    AND sum(
        CASE
            WHEN name = 'id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'case_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'user_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'role_type' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'effective_from' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'effective_to' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'created_by' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'created_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'ended_by' AND upper(type) = 'INTEGER' THEN 1
            ELSE 0
        END
    ) = 9
FROM pragma_table_info('student_case_assignments');

SELECT
    count(*) = 14
    AND sum(
        CASE
            WHEN name = 'id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'case_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'event_type' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'entity_type' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'entity_id' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'old_value_json' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'new_value_json' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'metadata_json' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'actor_type' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'actor_user_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'request_id' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'correlation_id' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'correction_of_event_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'created_at' AND upper(type) = 'TEXT' THEN 1
            ELSE 0
        END
    ) = 14
FROM pragma_table_info('case_events');

SELECT
    count(*) = 18
    AND sum(
        CASE
            WHEN name = 'id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'student_id' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'school_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'school_name_snapshot' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'institution_group_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'city_key' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'campus_key' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'school_sector' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'enrolment_role' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'course_name_snapshot' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'start_date' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'end_date' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'source_case_id' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'status' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'created_by' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'created_at' AND upper(type) = 'TEXT' THEN 1
            WHEN name = 'updated_by' AND upper(type) = 'INTEGER' THEN 1
            WHEN name = 'updated_at' AND upper(type) = 'TEXT' THEN 1
            ELSE 0
        END
    ) = 18
FROM pragma_table_info('student_school_enrolments');

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_student_cases_one_active_per_student'
  AND lower(sql) LIKE '%where active_slot = 1%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_student_case_assignments_one_current_owner'
  AND lower(sql) LIKE '%role_type = ''owner''%'
  AND lower(sql) LIKE '%effective_to is null%';

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'uq_student_case_assignments_current_role_user'
  AND lower(sql) LIKE '%effective_to is null%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type = 'index'
  AND name IN (
      'uq_student_school_enrolments_one_current',
      'uq_student_school_enrolments_one_upcoming'
  )
  AND lower(sql) NOT LIKE '%status%';

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

SELECT count(*) = 1
FROM sqlite_master
WHERE type = 'index'
  AND name = 'ix_case_events_case_created';

SELECT count(*) = 1
FROM pragma_foreign_key_list('student_case_sequences')
WHERE "from" = 'student_id'
  AND "table" = 'students'
  AND "to" = 'student_id';

SELECT count(*) = 3
FROM pragma_foreign_key_list('student_cases')
WHERE ("from" = 'student_id' AND "table" = 'students' AND "to" = 'student_id')
   OR ("from" = 'created_by' AND "table" = 'users' AND "to" = 'id')
   OR ("from" = 'updated_by' AND "table" = 'users' AND "to" = 'id');

SELECT count(*) = 4
FROM pragma_foreign_key_list('student_case_assignments')
WHERE ("from" = 'case_id' AND "table" = 'student_cases' AND "to" = 'id')
   OR ("from" = 'user_id' AND "table" = 'users' AND "to" = 'id')
   OR ("from" = 'created_by' AND "table" = 'users' AND "to" = 'id')
   OR ("from" = 'ended_by' AND "table" = 'users' AND "to" = 'id');

SELECT count(*) = 3
FROM pragma_foreign_key_list('case_events')
WHERE ("from" = 'case_id' AND "table" = 'student_cases' AND "to" = 'id')
   OR ("from" = 'actor_user_id' AND "table" = 'users' AND "to" = 'id')
   OR (
       "from" = 'correction_of_event_id'
       AND "table" = 'case_events'
       AND "to" = 'id'
   );

SELECT count(*) = 5
FROM pragma_foreign_key_list('student_school_enrolments')
WHERE ("from" = 'student_id' AND "table" = 'students' AND "to" = 'student_id')
   OR ("from" = 'school_id' AND "table" = 'schools' AND "to" = 'id')
   OR ("from" = 'source_case_id' AND "table" = 'student_cases' AND "to" = 'id')
   OR ("from" = 'created_by' AND "table" = 'users' AND "to" = 'id')
   OR ("from" = 'updated_by' AND "table" = 'users' AND "to" = 'id');

SELECT count(*) = 5
FROM sqlite_master
WHERE type = 'table'
  AND name IN (
      'student_case_sequences',
      'student_cases',
      'student_case_assignments',
      'case_events',
      'student_school_enrolments'
  )
  AND lower(sql) LIKE '%check%';

SELECT json_valid('{}') = 1;

SELECT count(*) = 0
FROM pragma_foreign_key_check
WHERE "table" IN (
    'student_case_sequences',
    'student_cases',
    'student_case_assignments',
    'case_events',
    'student_school_enrolments'
);

SELECT
    count(*) = 0
    OR (
        count(*) = 1
        AND min(name) = 'student_case_phase1'
        AND min(length(checksum_sha256)) = 64
    )
FROM schema_migrations
WHERE version = '20260728_001';
