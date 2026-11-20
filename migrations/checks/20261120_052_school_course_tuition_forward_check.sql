SELECT
 (SELECT count(*)=2 FROM sqlite_master WHERE type='table'
   AND name IN ('revenue_school_courses','revenue_school_course_evidence'))
 AND (SELECT count(*)=3 FROM sqlite_master WHERE type='index'
   AND name IN ('uq_school_course_identity','uq_school_course_offer_evidence','uq_school_course_coe_evidence'))
 AND (SELECT count(*)=4 FROM pragma_foreign_key_list('revenue_school_courses'))
 AND (SELECT count(*)=5 FROM pragma_foreign_key_list('revenue_school_course_evidence'));
