SELECT count(*) = 64
FROM sqlite_master
WHERE type='table' AND name LIKE 'revenue_%';

SELECT count(*) = 1
FROM revenue_module_state
WHERE id=1 AND status='disabled' AND cutover_at IS NULL;

SELECT count(*) = 0 FROM revenue_commercial_cases;
SELECT count(*) = 0 FROM revenue_commencement_facts;
SELECT count(*) = 0 FROM revenue_tuition_facts;
SELECT count(*) = 0 FROM revenue_fee_items;
SELECT count(*) = 0 FROM revenue_commission_lines;
SELECT count(*) = 0 FROM revenue_invoices;
SELECT count(*) = 0 FROM revenue_payments;
SELECT count(*) = 0 FROM revenue_ledger_entries;
SELECT count(*) = 0 FROM revenue_master_agent_statements;
SELECT count(*) = 0 FROM pragma_foreign_key_check;
SELECT quick_check = 'ok' FROM pragma_quick_check;
