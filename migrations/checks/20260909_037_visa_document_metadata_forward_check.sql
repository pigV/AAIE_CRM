SELECT CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_document_metadata')
AND EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_document_keyword_rules')
AND (SELECT count(*) FROM pragma_table_info('visa_document_metadata') WHERE name IN ('document_id','display_name','auto_category','manual_category','classification_evidence_json','updated_by','updated_at'))=7
AND (SELECT count(*) FROM pragma_table_info('visa_document_keyword_rules') WHERE name IN ('keyword','normalized_keyword','category','priority','active','created_by','created_at','updated_by','updated_at'))=9
THEN 1 ELSE 0 END;
