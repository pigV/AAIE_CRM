SELECT EXISTS (
    SELECT 1 FROM pragma_index_list('school_plan_reports')
    WHERE name='idx_school_plan_reports_workflow' AND "unique"=0 AND partial=0
)
AND (
    SELECT group_concat(name, ',')
    FROM (SELECT name FROM pragma_index_info('idx_school_plan_reports_workflow') ORDER BY seqno)
) = 'student_id,application_cycle_id,cycle_is_current,is_current,status,created_at';
