SELECT CASE WHEN
  (SELECT count(*) FROM pragma_table_info('progression_cases')
    WHERE name='business_type' AND upper(type)='TEXT'
      AND "notnull"=0 AND dflt_value IS NULL)=1
  AND
  (SELECT count(*) FROM pragma_table_info('progression_cases')
    WHERE name='source_contract_id' AND upper(type)='INTEGER'
      AND "notnull"=0 AND dflt_value IS NULL)=1
  AND
  (SELECT count(*) FROM pragma_foreign_key_list('progression_cases')
    WHERE "from"='source_contract_id' AND "table"='contracts'
      AND "to"='id' AND upper(on_delete)='RESTRICT')=1
  AND
  (SELECT count(*) FROM pragma_index_list('progression_cases')
    WHERE name='uq_progression_cases_source_contract' AND "unique"=1)=1
  AND
  (SELECT count(*) FROM sqlite_master
    WHERE type='index' AND name='uq_progression_cases_source_contract'
      AND upper(sql) LIKE '%WHERE SOURCE_CONTRACT_ID IS NOT NULL%')=1
  AND
  (SELECT count(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='progression_cases'
      AND name IN (
        'progression_contract_ownership_insert',
        'progression_contract_ownership_update'
      ))=2
  AND
  NOT EXISTS (
    SELECT 1 FROM progression_cases
    WHERE business_type IS NOT NULL OR source_contract_id IS NOT NULL
  )
  AND (SELECT quick_check FROM pragma_quick_check LIMIT 1)='ok'
THEN 1 ELSE 0 END;
