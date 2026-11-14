SELECT count(*) = 5
FROM pragma_table_info('final_school_coe_confirmations')
WHERE name IN ('final_acceptance_id','coe_id','evidence_snapshot','confirmed_by','confirmed_at')
  AND "notnull"=1;
