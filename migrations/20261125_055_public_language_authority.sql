-- migration_mode: sqlite_fk_off_rebuild
-- Existing course identities and all child evidence are retained byte-for-byte.
CREATE TABLE revenue_school_courses_rebuild_055 (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  commercial_case_id INTEGER NOT NULL REFERENCES revenue_commercial_cases(id) ON DELETE RESTRICT,
  course_kind TEXT NOT NULL CHECK(course_kind IN ('language','main')),
  school_id INTEGER REFERENCES schools(id) ON DELETE RESTRICT,
  course_name TEXT NOT NULL CHECK(length(trim(course_name))>0),
  enrolment_component_id INTEGER UNIQUE REFERENCES revenue_enrolment_components(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','withdrawn')),
  confirmed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  confirmed_at TEXT NOT NULL,
  authority_state TEXT CHECK(authority_state IS NULL OR authority_state IN ('ACT','NSW','NT','QLD','SA','TAS','VIC','WA')),
  CHECK(school_id IS NOT NULL OR (course_kind='language' AND authority_state IS NOT NULL))
);
INSERT INTO revenue_school_courses_rebuild_055(id,uid,commercial_case_id,course_kind,school_id,course_name,
 enrolment_component_id,status,confirmed_by,confirmed_at)
SELECT id,uid,commercial_case_id,course_kind,school_id,course_name,enrolment_component_id,status,confirmed_by,confirmed_at
FROM revenue_school_courses;
DROP TABLE revenue_school_courses;
ALTER TABLE revenue_school_courses_rebuild_055 RENAME TO revenue_school_courses;
CREATE UNIQUE INDEX uq_school_course_identity
 ON revenue_school_courses(commercial_case_id,course_kind,school_id,lower(trim(course_name))) WHERE status='active';
CREATE UNIQUE INDEX uq_public_language_authority_course
 ON revenue_school_courses(commercial_case_id,authority_state,lower(trim(course_name)))
 WHERE status='active' AND school_id IS NULL;
