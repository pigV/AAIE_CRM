SELECT
 (SELECT count(*)=1 FROM pragma_table_info('revenue_school_courses') WHERE name='authority_state')
 AND (SELECT "notnull"=0 FROM pragma_table_info('revenue_school_courses') WHERE name='school_id')
 AND (SELECT count(*)=1 FROM sqlite_master WHERE name='uq_public_language_authority_course' AND type='index')
 AND NOT EXISTS(SELECT 1 FROM revenue_school_courses WHERE school_id IS NULL
   AND (course_kind<>'language' OR authority_state IS NULL));
