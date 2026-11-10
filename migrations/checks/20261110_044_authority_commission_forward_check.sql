SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_authority_payers')
   AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_authority_invoices')
   AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_revenue_authority_invoice_line')
   AND EXISTS(SELECT 1 FROM pragma_table_info('offers') WHERE name='language_school_name');
