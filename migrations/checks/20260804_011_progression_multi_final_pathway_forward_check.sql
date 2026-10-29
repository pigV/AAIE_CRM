SELECT count(*) = 0 FROM sqlite_master
WHERE type='index' AND name='uq_progression_target_applications_one_final';

SELECT count(*) = 2 FROM sqlite_master
WHERE type='trigger' AND name IN (
  'progression_target_applications_max_three_final_insert',
  'progression_target_applications_max_three_final_update'
);
