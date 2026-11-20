-- Safe only before any course data is created. Fail before any DROP otherwise.
CREATE TEMP TABLE school_course_rollback_guard (empty INTEGER CHECK(empty=1));
INSERT INTO school_course_rollback_guard SELECT
  NOT EXISTS(SELECT 1 FROM revenue_school_courses)
  AND NOT EXISTS(SELECT 1 FROM revenue_school_course_evidence)
  AND NOT EXISTS(SELECT 1 FROM revenue_enrolment_components WHERE source_kind='school_language_course');
DROP TABLE revenue_school_course_evidence;
DROP TABLE revenue_school_courses;
DROP TABLE school_course_rollback_guard;
