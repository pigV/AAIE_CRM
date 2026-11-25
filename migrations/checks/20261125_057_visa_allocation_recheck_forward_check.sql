SELECT (SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='revenue_client_payment_allocations_validate' AND instr(sql,'v.archived_at IS NULL')>0)=1;
