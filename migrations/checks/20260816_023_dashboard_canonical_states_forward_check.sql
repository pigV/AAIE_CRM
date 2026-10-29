SELECT count(*) = 16 FROM pragma_table_info('offer_received_events');

SELECT count(*) = 20 FROM pragma_table_info('cycle_final_acceptances');

SELECT count(*) = 15 FROM pragma_table_info('visa_decision_events');

SELECT count(*) = 1
FROM sqlite_master
WHERE type='table' AND name='offer_received_event_state_events';

SELECT count(*) = 1
FROM sqlite_master
WHERE type='index' AND name='uq_cycle_final_acceptances_one_active'
  AND lower(sql) LIKE '%where acceptance_status=''active''%';

SELECT count(*) = 0
FROM (
  SELECT application_cycle_id
  FROM cycle_final_acceptances
  WHERE acceptance_status='active'
  GROUP BY application_cycle_id
  HAVING count(*) > 1
);

SELECT count(*) = 0 FROM pragma_foreign_key_check;

SELECT quick_check = 'ok' FROM pragma_quick_check;
