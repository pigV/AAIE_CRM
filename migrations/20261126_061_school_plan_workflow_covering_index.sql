-- Read workflow status from the index without fetching large report snapshots.
CREATE INDEX idx_school_plan_reports_workflow
ON school_plan_reports(student_id, application_cycle_id, cycle_is_current,
                       is_current, status, created_at);
