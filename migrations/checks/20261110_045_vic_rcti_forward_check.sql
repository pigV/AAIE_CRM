SELECT (SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN
    ('revenue_vic_rcti','revenue_vic_rcti_student','revenue_vic_rcti_bank','revenue_authority_payers','revenue_authority_invoices')) = 5
 AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_revenue_authority_invoice_line')
 AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_vic_rcti_bank_statement')
 AND EXISTS(SELECT 1 FROM pragma_table_info('offers') WHERE name='language_school_name');
