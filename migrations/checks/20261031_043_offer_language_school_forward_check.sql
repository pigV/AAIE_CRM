SELECT count(*) = 1
FROM pragma_table_info('offers')
WHERE name='language_school_name' AND upper(type)='TEXT' AND "notnull"=0;
