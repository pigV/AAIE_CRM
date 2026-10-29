SELECT count(*)=1 FROM sqlite_master WHERE type='table' AND name='revenue_document_locations';
SELECT count(*)=21 FROM pragma_table_info('revenue_document_locations');
SELECT count(*)=3 FROM sqlite_master WHERE type='trigger' AND name IN ('revenue_document_locations_validate','revenue_document_locations_no_update','revenue_document_locations_no_delete');
SELECT count(*)=2 FROM sqlite_master WHERE type='trigger' AND name IN ('revenue_documents_financial_no_update','revenue_documents_financial_no_delete');
SELECT count(*)=0 FROM revenue_document_locations;
SELECT count(*)=0 FROM pragma_foreign_key_check;
SELECT quick_check='ok' FROM pragma_quick_check;
