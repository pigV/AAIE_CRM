SELECT count(*) = 1
FROM pragma_table_info('offers')
WHERE name='course_start_date' AND upper(type)='TEXT';
