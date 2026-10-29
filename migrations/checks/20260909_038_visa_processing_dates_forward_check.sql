SELECT CASE WHEN
EXISTS(SELECT 1 FROM pragma_table_info('visa_processing_date_extractions') WHERE name='proposed_json') AND
EXISTS(SELECT 1 FROM pragma_table_info('visa_processing_date_revisions') WHERE name='before_json') AND
(SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ('processing_extractions_no_update','processing_extractions_no_delete','processing_revisions_no_update','processing_revisions_no_delete'))=4
THEN 1 ELSE 0 END AS ok;
