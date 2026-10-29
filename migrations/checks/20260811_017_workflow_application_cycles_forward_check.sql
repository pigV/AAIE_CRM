SELECT count(*) = 9 FROM sqlite_master
WHERE type='table' AND name IN (
  'application_cycles',
  'public_application_groups',
  'public_application_preferences',
  'private_school_applications',
  'offer_received_events',
  'cycle_final_acceptances',
  'cycle_final_acceptance_history',
  'final_school_submission_events',
  'visa_decision_events'
);

SELECT count(*) = 1 FROM pragma_table_info('contracts')
WHERE name='application_cycle_id';

SELECT count(*) = 2 FROM pragma_table_info('school_plan_reports')
WHERE name IN ('application_cycle_id','cycle_is_current');

SELECT count(*) = 1 FROM pragma_table_info('offers')
WHERE name='application_cycle_id';

SELECT count(*) = 1 FROM pragma_table_info('coe_oshc')
WHERE name='application_cycle_id';

SELECT count(*) = 1 FROM pragma_table_info('visa_cases')
WHERE name='application_cycle_id';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_application_cycles_active_student';

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_school_plan_cycle_current_public_city';

SELECT count(*) = 6 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'application_cycles_student_attach_once',
  'application_cycles_no_delete',
  'offer_received_events_no_update',
  'offer_received_events_no_delete',
  'visa_decision_events_no_update',
  'visa_decision_events_no_delete'
);

SELECT count(*) = 0 FROM application_cycles
WHERE (status='active' AND (outcome IS NOT NULL OR closed_at IS NOT NULL))
   OR (status='closed' AND (outcome IS NULL OR closed_at IS NULL));

SELECT count(*) = 0
FROM application_cycles AS cycle
JOIN (
  SELECT student_id FROM application_cycles
  WHERE status='active' AND student_id IS NOT NULL
  GROUP BY student_id HAVING count(*) > 1
) AS duplicate ON duplicate.student_id=cycle.student_id;

SELECT count(*) = 0
FROM school_plan_reports
WHERE application_cycle_id IS NULL AND cycle_is_current<>0;

SELECT count(*) = 0
FROM offer_received_events
WHERE (source_kind='public' AND (
        public_application_group_id IS NULL OR private_application_id IS NOT NULL
      ))
   OR (source_kind='private' AND (
        private_application_id IS NULL OR public_application_group_id IS NOT NULL
      ));

SELECT count(*) = 0
FROM visa_decision_events AS decision
JOIN application_cycles AS cycle ON cycle.id=decision.application_cycle_id
WHERE cycle.student_id<>decision.student_id;

SELECT count(*) = 0 FROM pragma_foreign_key_check;
