SELECT EXISTS (
    SELECT 1 FROM pragma_table_info('coe_oshc') WHERE name='superseded_by_coe_id'
) AND EXISTS (
    SELECT 1 FROM pragma_foreign_key_list('coe_oshc')
    WHERE "from"='superseded_by_coe_id' AND "table"='coe_oshc' AND "to"='id'
) AND NOT EXISTS (
    SELECT 1
    FROM coe_oshc AS previous JOIN coe_oshc AS current ON current.id=previous.superseded_by_coe_id
    WHERE current.student_id IS NOT previous.student_id
       OR current.application_cycle_id IS NOT previous.application_cycle_id
);
