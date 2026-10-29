SELECT count(*) = 5 FROM sqlite_master
WHERE type='table' AND name IN (
  'school_plan_confirmed_schools',
  'school_plan_school_submission_events',
  'student_offer_acceptances',
  'offer_acceptance_history',
  'school_plan_offer_migration_exceptions'
);

SELECT count(*) = 6 FROM pragma_table_info('school_plan_reports')
WHERE name IN (
  'city_id','city_name_snapshot','plan_sector','is_current',
  'superseded_by_report_id','scope_review_status'
);

SELECT count(*) = 5 FROM pragma_table_info('offers')
WHERE name IN (
  'school_id','city_id','city_name_snapshot','sector_key','classification_status'
);

SELECT count(*) = 18 FROM pragma_table_info('school_plan_confirmed_schools');

SELECT count(*) = 11 FROM pragma_table_info('school_plan_school_submission_events');

SELECT count(*) = 7 FROM pragma_table_info('student_offer_acceptances');

SELECT count(*) = 12 FROM pragma_table_info('offer_acceptance_history');

SELECT count(*) = 11 FROM pragma_table_info('school_plan_offer_migration_exceptions');

SELECT count(*) = 2 FROM pragma_foreign_key_list('school_plan_reports')
WHERE ([table]='school_cities' AND [from]='city_id' AND [to]='id')
   OR ([table]='school_plan_reports' AND [from]='superseded_by_report_id' AND [to]='id');

SELECT count(*) = 2 FROM pragma_foreign_key_list('offers')
WHERE ([table]='schools' AND [from]='school_id' AND [to]='id')
   OR ([table]='school_cities' AND [from]='city_id' AND [to]='id');

SELECT count(*) = 6 FROM pragma_foreign_key_list('school_plan_confirmed_schools')
WHERE ([table]='school_plan_report_confirmations' AND [from]='confirmation_id' AND [to]='id' AND on_delete='CASCADE')
   OR ([table]='school_plan_reports' AND [from]='report_id' AND [to]='id' AND on_delete='CASCADE')
   OR ([table]='students' AND [from]='student_id' AND [to]='student_id')
   OR ([table]='schools' AND [from]='school_id' AND [to]='id')
   OR ([table]='school_cities' AND [from]='city_id' AND [to]='id')
   OR ([table]='users' AND [from]='submitted_by_user_id' AND [to]='id');

SELECT count(*) = 4 FROM pragma_foreign_key_list('school_plan_school_submission_events')
WHERE ([table]='school_plan_confirmed_schools' AND [from]='confirmed_school_id' AND [to]='id' AND on_delete='RESTRICT')
   OR ([table]='students' AND [from]='student_id' AND [to]='student_id')
   OR ([table]='school_plan_reports' AND [from]='report_id' AND [to]='id' AND on_delete='RESTRICT')
   OR ([table]='users' AND [from]='submitted_by_user_id' AND [to]='id');

SELECT count(*) = 3 FROM pragma_foreign_key_list('student_offer_acceptances')
WHERE ([table]='students' AND [from]='student_id' AND [to]='student_id' AND on_delete='CASCADE')
   OR ([table]='offers' AND [from]='offer_id' AND [to]='id' AND on_delete='CASCADE')
   OR ([table]='users' AND [from]='accepted_by_user_id' AND [to]='id');

SELECT count(*) = 4 FROM pragma_foreign_key_list('offer_acceptance_history')
WHERE ([table]='students' AND [from]='student_id' AND [to]='student_id')
   OR ([table]='offers' AND [from]='previous_offer_id' AND [to]='id' AND on_delete='RESTRICT')
   OR ([table]='offers' AND [from]='new_offer_id' AND [to]='id' AND on_delete='RESTRICT')
   OR ([table]='users' AND [from]='accepted_by_user_id' AND [to]='id');

SELECT instr(sql, "plan_sector IN ('public','private')") > 0
FROM sqlite_master WHERE type='table' AND name='school_plan_reports';

SELECT instr(sql, "scope_review_status IN ('pending','resolved','needs_review')") > 0
FROM sqlite_master WHERE type='table' AND name='school_plan_reports';

SELECT instr(sql, "sector_key IN ('public','private')") > 0
FROM sqlite_master WHERE type='table' AND name='offers';

SELECT instr(sql, "classification_status IN ('resolved','unresolved','ambiguous')") > 0
FROM sqlite_master WHERE type='table' AND name='offers';

SELECT instr(sql, "plan_sector IN ('public','private')") > 0
FROM sqlite_master WHERE type='table' AND name='school_plan_confirmed_schools';

SELECT instr(sql, "decision_mode IN ('ranked','selected')") > 0
FROM sqlite_master WHERE type='table' AND name='school_plan_confirmed_schools';

SELECT instr(sql, "submission_status IN ('not_submitted','submitted')") > 0
FROM sqlite_master WHERE type='table' AND name='school_plan_confirmed_schools';

SELECT instr(sql, "coe_present IN (0,1)") > 0
FROM sqlite_master WHERE type='table' AND name='offer_acceptance_history';

SELECT instr(sql, "coe_warning_acknowledged IN (0,1)") > 0
FROM sqlite_master WHERE type='table' AND name='offer_acceptance_history';

SELECT count(*) = 1 FROM pragma_table_info('student_offer_acceptances')
WHERE name='student_id' AND pk=1;

SELECT count(*) = 5 FROM pragma_table_info('offer_acceptance_history')
WHERE name IN (
  'student_id','previous_offer_id','new_offer_id',
  'previous_offer_restored_status','new_offer_previous_status'
);

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_school_plan_current_public_city'
  AND instr(sql, "ON school_plan_reports(student_id, city_id)") > 0
  AND instr(sql, "WHERE plan_sector='public' AND is_current=1") > 0;

SELECT count(*) = 1 FROM sqlite_master
WHERE type='index' AND name='uq_school_plan_confirmed_schools_null_school'
  AND instr(sql, "ON school_plan_confirmed_schools(confirmation_id, school_name_snapshot)") > 0
  AND instr(sql, "WHERE school_id IS NULL") > 0;

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'school_plan_confirmed_schools_ownership_insert',
  'school_plan_confirmed_schools_ownership_update'
)
AND instr(sql, 'school_plan_report_confirmations') > 0
AND instr(sql, 'school_plan_reports') > 0;

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'student_offer_acceptances_ownership_insert',
  'student_offer_acceptances_ownership_update'
)
AND instr(sql, 'offers') > 0;

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'school_plan_school_submission_events_ownership_insert',
  'school_plan_school_submission_events_ownership_update'
)
AND instr(sql, 'school_plan_confirmed_schools') > 0;

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'offer_acceptance_history_ownership_insert',
  'offer_acceptance_history_ownership_update'
)
AND instr(sql, 'offers') > 0;

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='school_plan_confirmed_schools_submission_event_after_update'
  AND instr(sql, 'AFTER UPDATE OF submission_status') > 0
  AND instr(sql, 'OLD.submission_status <> NEW.submission_status') > 0
  AND instr(sql, 'INSERT INTO school_plan_school_submission_events') > 0;

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'school_plan_confirmed_schools_not_submitted_evidence_insert',
  'school_plan_confirmed_schools_not_submitted_evidence_update'
)
AND instr(sql, "NEW.submission_status='not_submitted'") > 0
AND instr(sql, 'submitted_at IS NOT NULL') > 0
AND instr(sql, 'submitted_by_user_id IS NOT NULL') > 0
AND instr(sql, 'submitted_by_name IS NOT NULL') > 0;

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='school_plan_confirmed_schools_identity_no_update_after_event'
  AND instr(sql, 'BEFORE UPDATE OF confirmation_id,report_id,student_id') > 0
  AND instr(sql, 'school_plan_school_submission_events') > 0;

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='offers_student_identity_no_update'
  AND instr(sql, 'BEFORE UPDATE OF student_id ON offers') > 0
  AND instr(sql, 'student_offer_acceptances') > 0
  AND instr(sql, 'offer_acceptance_history') > 0;

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='school_plan_reports_student_identity_no_update'
  AND instr(sql, 'BEFORE UPDATE OF student_id ON school_plan_reports') > 0
  AND instr(sql, 'school_plan_report_confirmations') > 0
  AND instr(sql, 'school_plan_confirmed_schools') > 0
  AND instr(sql, 'school_plan_school_submission_events') > 0;

SELECT count(*) = 1 FROM sqlite_master
WHERE type='trigger' AND name='school_plan_report_confirmations_report_identity_no_update'
  AND instr(sql, 'BEFORE UPDATE OF report_id ON school_plan_report_confirmations') > 0
  AND instr(sql, 'school_plan_confirmed_schools') > 0;

SELECT count(*) = 4 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'school_plan_school_submission_events_no_update',
  'school_plan_school_submission_events_no_delete',
  'offer_acceptance_history_no_update',
  'offer_acceptance_history_no_delete'
);
