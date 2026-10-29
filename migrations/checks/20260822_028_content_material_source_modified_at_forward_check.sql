SELECT CASE WHEN
  (SELECT count(*)
     FROM pragma_table_info('content_materials')
    WHERE name='source_modified_at'
      AND upper(type)='TEXT'
      AND "notnull"=0
      AND dflt_value IS NULL)=1
  AND (SELECT count(*)
         FROM pragma_index_list('content_materials')
        WHERE name='idx_content_materials_source_modified_at'
          AND "unique"=0)=1
  AND (SELECT count(*)
         FROM pragma_index_xinfo('idx_content_materials_source_modified_at')
        WHERE "key"=1)=2
  AND (SELECT count(*)
         FROM pragma_index_xinfo('idx_content_materials_source_modified_at')
        WHERE "key"=1 AND seqno=0 AND name='source_modified_at' AND "desc"=1)=1
  AND (SELECT count(*)
         FROM pragma_index_xinfo('idx_content_materials_source_modified_at')
        WHERE "key"=1 AND seqno=1 AND name='id' AND "desc"=1)=1
  AND (SELECT count(*)
         FROM pragma_index_list('content_materials')
        WHERE name='idx_content_materials_script_id_id'
          AND "unique"=0)=1
  AND (SELECT count(*)
         FROM pragma_index_xinfo('idx_content_materials_script_id_id')
        WHERE "key"=1)=2
  AND (SELECT count(*)
         FROM pragma_index_xinfo('idx_content_materials_script_id_id')
        WHERE "key"=1 AND seqno=0 AND name='script_id' AND "desc"=0)=1
  AND (SELECT count(*)
         FROM pragma_index_xinfo('idx_content_materials_script_id_id')
        WHERE "key"=1 AND seqno=1 AND name='id' AND "desc"=1)=1
  AND NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check)
  AND (SELECT quick_check FROM pragma_quick_check LIMIT 1)='ok'
THEN 1 ELSE 0 END;
