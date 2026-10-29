SELECT count(*) = 1 FROM sqlite_master
WHERE type='table' AND name='school_plan_school_result_events';

SELECT count(*) = 6 FROM pragma_table_info('school_plan_confirmed_schools')
WHERE name IN (
  'result_status','result_at','result_by_user_id',
  'result_by_name','result_note','result_offer_id'
);

SELECT count(*) = 3 FROM pragma_table_info('visa_cases')
WHERE name IN ('lodged_by_user_id','lodged_by_name','lodgement_recorded_at');

SELECT count(*) = 13 FROM pragma_table_info('school_plan_school_result_events');

SELECT count(*) = 2 FROM sqlite_master
WHERE type='index' AND name IN (
  'idx_school_plan_confirmed_schools_result',
  'idx_school_plan_result_events_school'
);

SELECT count(*) = 7 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'school_plan_school_result_evidence_insert',
  'school_plan_school_result_evidence_update',
  'school_plan_school_result_offer_ownership_insert',
  'school_plan_school_result_offer_ownership_update',
  'school_plan_school_result_event_after_update',
  'school_plan_school_result_events_no_update',
  'school_plan_school_result_events_no_delete'
);

SELECT count(*) = 3 FROM pragma_foreign_key_list('school_plan_confirmed_schools')
WHERE ([table]='users' AND [from]='result_by_user_id' AND [to]='id')
   OR ([table]='offers' AND [from]='result_offer_id' AND [to]='id')
   OR ([table]='users' AND [from]='submitted_by_user_id' AND [to]='id');

SELECT count(*) = 5 FROM pragma_foreign_key_list('school_plan_school_result_events')
WHERE ([table]='school_plan_confirmed_schools' AND [from]='confirmed_school_id' AND [to]='id' AND on_delete='RESTRICT')
   OR ([table]='students' AND [from]='student_id' AND [to]='student_id')
   OR ([table]='school_plan_reports' AND [from]='report_id' AND [to]='id' AND on_delete='RESTRICT')
   OR ([table]='users' AND [from]='result_by_user_id' AND [to]='id')
   OR ([table]='offers' AND [from]='result_offer_id' AND [to]='id');

SELECT count(*) = 0
FROM school_plan_confirmed_schools
WHERE (result_status='pending' AND (
         result_at IS NOT NULL OR result_by_user_id IS NOT NULL OR
         result_by_name IS NOT NULL OR result_note IS NOT NULL OR result_offer_id IS NOT NULL
       ))
   OR (result_status<>'pending' AND (
         submission_status<>'submitted' OR result_at IS NULL OR
         result_by_user_id IS NULL OR trim(coalesce(result_by_name,''))=''
       ))
   OR (result_status='offer_received' AND result_offer_id IS NULL)
   OR (result_status<>'offer_received' AND result_offer_id IS NOT NULL)
   OR (result_status IN ('rejected','no_place_or_closed','withdrawn','voided')
       AND trim(coalesce(result_note,''))='');

SELECT count(*) = 0
FROM school_plan_confirmed_schools AS confirmed
JOIN offers ON offers.id=confirmed.result_offer_id
WHERE offers.student_id<>confirmed.student_id;
