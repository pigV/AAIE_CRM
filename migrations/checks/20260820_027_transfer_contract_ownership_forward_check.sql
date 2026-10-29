SELECT CASE WHEN
  (SELECT count(*) FROM pragma_table_info('progression_cases')
    WHERE name='current_school_enrolment_id' AND upper(type)='INTEGER'
      AND "notnull"=0)=1
  AND (SELECT count(*) FROM pragma_foreign_key_list('progression_cases')
    WHERE "from"='current_school_enrolment_id'
      AND "table"='student_school_enrolments' AND "to"='id'
      AND upper(on_delete)='RESTRICT')=1
  AND (SELECT count(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='progression_cases' AND name IN (
      'progression_current_enrolment_ownership_insert',
      'progression_current_enrolment_ownership_update'
    ))=2
  AND (SELECT count(*) FROM sqlite_master
    WHERE type='table' AND name='application_cycles'
      AND sql LIKE '%superseded_by_transfer%'
      AND sql LIKE '%superseded_by_progression%')=1
  AND (SELECT count(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='application_cycles' AND name IN (
      'application_cycles_case_ownership_insert',
      'application_cycles_case_ownership_update',
      'application_cycles_student_attach_once',
      'application_cycles_no_delete'
    ))=4
  AND
  (SELECT count(*) FROM pragma_table_info('transfer_cases')
    WHERE name='source_contract_id' AND upper(type)='INTEGER'
      AND "notnull"=0)=1
  AND (SELECT count(*) FROM pragma_foreign_key_list('transfer_cases')
    WHERE "from"='source_contract_id' AND "table"='contracts'
      AND "to"='id' AND upper(on_delete)='RESTRICT')=1
  AND (SELECT count(*) FROM pragma_index_list('transfer_cases')
    WHERE name='uq_transfer_cases_external_contract' AND "unique"=1)=1
  AND (SELECT count(*) FROM sqlite_master
    WHERE type='index' AND name='uq_transfer_cases_external_contract'
      AND upper(sql) LIKE '%WHERE SOURCE_CONTRACT_ID IS NOT NULL%')=1
  AND (SELECT count(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='transfer_cases' AND name IN (
      'transfer_cases_contract_ownership_insert',
      'transfer_cases_contract_ownership_update',
      'transfer_cases_relationship_validate_insert',
      'transfer_cases_relationship_validate_update',
      'transfer_cases_require_transfer_parent_insert',
      'transfer_cases_require_transfer_parent_update',
      'transfer_cases_final_application_insert_guard',
      'transfer_cases_final_application_update_guard',
      'transfer_cases_no_delete'
    ))=9
  AND (SELECT count(*) FROM sqlite_master
    WHERE type='trigger'
      AND tbl_name='student_school_enrolments'
      AND name='student_school_enrolments_transfer_reference_guard'
      AND upper(sql) LIKE '%PARENT_CASE.ACTIVE_SLOT=0%'
      AND upper(sql) LIKE '%COMPLETED%'
      AND upper(sql) LIKE '%CANCELLED%'
      AND upper(sql) LIKE '%FAILED%'
      AND upper(sql) LIKE '%ARCHIVED%')=1
  AND NOT EXISTS (
    SELECT 1
    FROM transfer_cases AS transfer
    JOIN student_cases AS student_case ON student_case.id=transfer.case_id
    LEFT JOIN contracts AS contract ON contract.id=transfer.source_contract_id
    WHERE
      (transfer.transfer_origin='external' AND transfer.source_contract_id IS NULL)
      OR (
        transfer.source_contract_id IS NOT NULL AND (
          contract.id IS NULL
          OR contract.student_id IS NOT student_case.student_id
          OR contract.contract_type IS NOT 'TransferApplicationContract'
          OR contract.contract_status IS NOT 'FullyExecuted'
          OR contract.deleted_at IS NOT NULL
          OR contract.voided_at IS NOT NULL
        )
      )
  )
  AND NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check)
  AND (SELECT quick_check FROM pragma_quick_check LIMIT 1)='ok'
  AND (SELECT integrity_check FROM pragma_integrity_check LIMIT 1)='ok'
THEN 1 ELSE 0 END;
