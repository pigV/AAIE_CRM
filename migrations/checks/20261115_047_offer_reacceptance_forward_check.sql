SELECT
  EXISTS (SELECT 1 FROM pragma_index_list('cycle_final_acceptances')
           WHERE name='uq_cycle_final_acceptances_one_active' AND "unique"=1 AND partial=1)
  AND EXISTS (SELECT 1 FROM pragma_index_list('cycle_final_acceptances')
               WHERE name='idx_cycle_final_acceptances_offer' AND "unique"=0)
  AND NOT EXISTS (SELECT 1 FROM pragma_index_list('cycle_final_acceptances') WHERE origin='u')
  AND (SELECT count(*)=5 FROM sqlite_master
        WHERE type='trigger' AND tbl_name='cycle_final_acceptances');
