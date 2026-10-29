SELECT count(*) = 1 FROM sqlite_master
WHERE type='table' AND name='school_enrolment_transition_intents';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='table' AND name='school_enrolment_events';

SELECT count(*) = 6 FROM pragma_table_info('student_school_enrolments')
WHERE name IN (
  'application_cycle_id','year_level_snapshot','lifecycle_context',
  'confirmation_reason','confirmed_by','confirmed_at'
);

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_student_school_enrolments_one_current'
  AND lower(sql) NOT LIKE '%status%';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_student_school_enrolments_one_upcoming'
  AND lower(sql) NOT LIKE '%status%';

SELECT count(*) = 0 FROM school_enrolment_transition_intents;
SELECT count(*) = 0 FROM school_enrolment_events;
SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
