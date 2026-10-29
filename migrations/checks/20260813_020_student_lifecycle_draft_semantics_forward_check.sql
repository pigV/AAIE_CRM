SELECT count(*) = 17 FROM pragma_table_info('student_cases');

SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='student_lifecycle_idempotency_receipts';

SELECT count(*) = 10
FROM pragma_table_info('student_lifecycle_idempotency_receipts');

SELECT count(*) = 1
FROM sqlite_master
WHERE type='index' AND name='uq_student_cases_one_active_per_student'
  AND lower(sql) LIKE '%where active_slot=1%';

SELECT count(*) = 2
FROM sqlite_master
WHERE type='trigger' AND name IN (
  'student_cases_no_delete','student_cases_archive_from_terminal_only'
);

SELECT count(*) = 0
FROM student_cases
WHERE (status='draft' AND active_slot<>0)
   OR (status IN ('preparing','in_progress','waiting','paused') AND active_slot<>1)
   OR (status IN ('completed','cancelled','failed','archived') AND active_slot<>0);

SELECT count(*) = 0 FROM pragma_foreign_key_check;

SELECT quick_check = 'ok' FROM pragma_quick_check;
