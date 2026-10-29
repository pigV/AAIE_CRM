ALTER TABLE students ADD COLUMN higher_education_package_combination TEXT;
ALTER TABLE students ADD COLUMN parent_requested_pathway TEXT;

ALTER TABLE higher_education_providers ADD COLUMN short_name TEXT;
ALTER TABLE higher_education_providers ADD COLUMN state TEXT;
ALTER TABLE higher_education_providers ADD COLUMN city TEXT;

CREATE TABLE higher_education_pathway_combinations (
  id INTEGER PRIMARY KEY,
  university_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  pathway_code TEXT NOT NULL CHECK(length(trim(pathway_code)) > 0),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
  final_course_level TEXT NOT NULL CHECK(final_course_level IN ('Foundation','Diploma','Bachelor','Master')),
  active_status TEXT NOT NULL DEFAULT 'Active' CHECK(active_status IN ('Active','Inactive')),
  source_url TEXT,
  evidence TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  UNIQUE(university_id,pathway_code)
);
CREATE INDEX idx_he_pathways_university_active
  ON higher_education_pathway_combinations(university_id,active_status,display_name);

CREATE TABLE higher_education_pathway_segments (
  id INTEGER PRIMARY KEY,
  pathway_combination_id INTEGER NOT NULL REFERENCES higher_education_pathway_combinations(id) ON DELETE RESTRICT,
  segment_order INTEGER NOT NULL CHECK(segment_order > 0),
  provider_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  course_level TEXT NOT NULL CHECK(course_level IN ('Foundation','Diploma','Bachelor','Master')),
  target_university_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  source_url TEXT,
  evidence TEXT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  UNIQUE(pathway_combination_id,segment_order)
);
CREATE INDEX idx_he_pathway_segments_provider
  ON higher_education_pathway_segments(provider_id,pathway_combination_id,segment_order);

CREATE TABLE higher_education_courses (
  id INTEGER PRIMARY KEY,
  university_id INTEGER NOT NULL REFERENCES higher_education_providers(id) ON DELETE RESTRICT,
  course_name TEXT NOT NULL CHECK(length(trim(course_name)) > 0),
  normalized_name TEXT NOT NULL CHECK(length(trim(normalized_name)) > 0),
  course_level TEXT NOT NULL CHECK(course_level IN ('Foundation','Diploma','Bachelor','Master','Postgraduate')),
  specialisation TEXT,
  active_status TEXT NOT NULL DEFAULT 'Active' CHECK(active_status IN ('Active','Inactive')),
  merged_into_course_id INTEGER REFERENCES higher_education_courses(id) ON DELETE RESTRICT,
  created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
  updated_at TEXT NOT NULL,
  UNIQUE(university_id,normalized_name,course_level),
  CHECK(merged_into_course_id IS NULL OR merged_into_course_id <> id)
);
CREATE INDEX idx_he_courses_university_active_name
  ON higher_education_courses(university_id,active_status,normalized_name);

ALTER TABLE higher_education_plans ADD COLUMN default_pathway_code TEXT;
ALTER TABLE higher_education_plan_items ADD COLUMN pathway_combination_id INTEGER REFERENCES higher_education_pathway_combinations(id) ON DELETE RESTRICT;
ALTER TABLE higher_education_plan_items ADD COLUMN course_id INTEGER REFERENCES higher_education_courses(id) ON DELETE RESTRICT;
ALTER TABLE higher_education_applications ADD COLUMN pathway_combination_id INTEGER REFERENCES higher_education_pathway_combinations(id) ON DELETE RESTRICT;
ALTER TABLE higher_education_applications ADD COLUMN course_id INTEGER REFERENCES higher_education_courses(id) ON DELETE RESTRICT;
ALTER TABLE higher_education_package_segments ADD COLUMN target_university_id INTEGER REFERENCES higher_education_providers(id) ON DELETE RESTRICT;
ALTER TABLE higher_education_package_segments ADD COLUMN course_id INTEGER REFERENCES higher_education_courses(id) ON DELETE RESTRICT;
ALTER TABLE higher_education_package_segments ADD COLUMN is_final_degree INTEGER NOT NULL DEFAULT 0 CHECK(is_final_degree IN (0,1));

CREATE TRIGGER higher_education_pathway_university_insert
BEFORE INSERT ON higher_education_pathway_combinations
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_providers
  WHERE id=NEW.university_id AND provider_type='University'
)
BEGIN SELECT RAISE(ABORT, 'higher education pathway requires a university'); END;

CREATE TRIGGER higher_education_pathway_university_update
BEFORE UPDATE OF university_id ON higher_education_pathway_combinations
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_providers
  WHERE id=NEW.university_id AND provider_type='University'
)
BEGIN SELECT RAISE(ABORT, 'higher education pathway requires a university'); END;

CREATE TRIGGER higher_education_pathway_segment_scope_insert
BEFORE INSERT ON higher_education_pathway_segments
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_pathway_combinations c
  JOIN higher_education_providers u ON u.id=NEW.target_university_id AND u.provider_type='University'
  JOIN higher_education_providers p ON p.id=NEW.provider_id AND p.active_status='Active'
  WHERE c.id=NEW.pathway_combination_id AND c.university_id=NEW.target_university_id
    AND (p.id=u.id OR EXISTS(
      SELECT 1 FROM higher_education_provider_relationships r
      WHERE r.university_id=u.id AND r.pathway_provider_id=p.id AND r.active_status='Active'
    ))
)
BEGIN SELECT RAISE(ABORT, 'pathway segment is outside university architecture'); END;

CREATE TRIGGER higher_education_pathway_segment_scope_update
BEFORE UPDATE OF pathway_combination_id,provider_id,target_university_id ON higher_education_pathway_segments
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_pathway_combinations c
  JOIN higher_education_providers u ON u.id=NEW.target_university_id AND u.provider_type='University'
  JOIN higher_education_providers p ON p.id=NEW.provider_id AND p.active_status='Active'
  WHERE c.id=NEW.pathway_combination_id AND c.university_id=NEW.target_university_id
    AND (p.id=u.id OR EXISTS(
      SELECT 1 FROM higher_education_provider_relationships r
      WHERE r.university_id=u.id AND r.pathway_provider_id=p.id AND r.active_status='Active'
    ))
)
BEGIN SELECT RAISE(ABORT, 'pathway segment is outside university architecture'); END;

CREATE TRIGGER higher_education_course_university_insert
BEFORE INSERT ON higher_education_courses
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_providers
  WHERE id=NEW.university_id AND provider_type='University'
)
BEGIN SELECT RAISE(ABORT, 'higher education course requires a university'); END;

CREATE TRIGGER higher_education_course_university_update
BEFORE UPDATE OF university_id ON higher_education_courses
WHEN NOT EXISTS(
  SELECT 1 FROM higher_education_providers
  WHERE id=NEW.university_id AND provider_type='University'
)
BEGIN SELECT RAISE(ABORT, 'higher education course requires a university'); END;

CREATE TRIGGER higher_education_plan_item_v2_scope_insert
BEFORE INSERT ON higher_education_plan_items
WHEN NEW.pathway_combination_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_pathway_combinations c
  WHERE c.id=NEW.pathway_combination_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'plan item pathway is outside selected university'); END;

CREATE TRIGGER higher_education_plan_item_v2_scope_update
BEFORE UPDATE OF provider_id,pathway_combination_id ON higher_education_plan_items
WHEN NEW.pathway_combination_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_pathway_combinations c
  WHERE c.id=NEW.pathway_combination_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'plan item pathway is outside selected university'); END;

CREATE TRIGGER higher_education_plan_item_course_scope_insert
BEFORE INSERT ON higher_education_plan_items
WHEN NEW.course_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_courses c
  WHERE c.id=NEW.course_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'plan item course is outside selected university'); END;

CREATE TRIGGER higher_education_plan_item_course_scope_update
BEFORE UPDATE OF provider_id,course_id ON higher_education_plan_items
WHEN NEW.course_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_courses c
  WHERE c.id=NEW.course_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'plan item course is outside selected university'); END;

CREATE TRIGGER higher_education_application_v2_scope_insert
BEFORE INSERT ON higher_education_applications
WHEN NEW.pathway_combination_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_pathway_combinations c
  WHERE c.id=NEW.pathway_combination_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'application pathway is outside selected university'); END;

CREATE TRIGGER higher_education_application_v2_scope_update
BEFORE UPDATE OF provider_id,pathway_combination_id ON higher_education_applications
WHEN NEW.pathway_combination_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_pathway_combinations c
  WHERE c.id=NEW.pathway_combination_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'application pathway is outside selected university'); END;

CREATE TRIGGER higher_education_application_course_scope_insert
BEFORE INSERT ON higher_education_applications
WHEN NEW.course_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_courses c
  WHERE c.id=NEW.course_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'application course is outside selected university'); END;

CREATE TRIGGER higher_education_application_course_scope_update
BEFORE UPDATE OF provider_id,course_id ON higher_education_applications
WHEN NEW.course_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM higher_education_courses c
  WHERE c.id=NEW.course_id AND c.university_id=NEW.provider_id AND c.active_status='Active'
)
BEGIN SELECT RAISE(ABORT, 'application course is outside selected university'); END;

-- Maintainable official initial pathway architecture, verified 2026-08-13.
-- These records are seed data only; application logic never hard-codes them.
INSERT OR IGNORE INTO higher_education_providers(
  provider_name,normalized_name,provider_type,review_status,active_status,source_url,evidence,
  short_name,state,city,created_at,updated_at
) VALUES
('Deakin College','deakin college','Pathway Provider','Approved','Active',
 'https://www.deakincollege.edu.au/courses/foundation-program/',
 'Official Deakin College source documents Foundation and Diploma progression to Deakin University',
 'Deakin College','VIC','Melbourne','2026-08-13','2026-08-13'),
('Taylors College Sydney','taylors college sydney','Pathway Provider','Approved','Active',
 'https://www.sydney.edu.au/study/applying/how-to-apply/international-students/preparation-programs.html',
 'University of Sydney states its preparation programs are delivered at Taylors College',
 'Taylors College','NSW','Sydney','2026-08-13','2026-08-13');

INSERT OR IGNORE INTO higher_education_provider_relationships(
  university_id,pathway_provider_id,relationship_type,effective_from,active_status,
  source_url,evidence,created_at,updated_at
) VALUES
((SELECT id FROM higher_education_providers WHERE normalized_name='deakin university'),
 (SELECT id FROM higher_education_providers WHERE normalized_name='deakin college'),
 'Partner','2026-08-13','Active','https://www.deakincollege.edu.au/current-students/essential-information/transferring-to-deakin-university/',
 'Official Deakin College transfer relationship','2026-08-13','2026-08-13'),
((SELECT id FROM higher_education_providers WHERE normalized_name='university of sydney'),
 (SELECT id FROM higher_education_providers WHERE normalized_name='taylors college sydney'),
 'Partner','2026-08-13','Active','https://www.sydney.edu.au/study/applying/how-to-apply/international-students/preparation-programs.html',
 'University of Sydney official preparation-program delivery relationship','2026-08-13','2026-08-13');

WITH pathway_seed(university_name,pathway_code,display_name,final_course_level,source_url,evidence) AS (
  VALUES
  ('monash university','direct-bachelor','Direct Bachelor','Bachelor','https://www.monash.edu/study/courses','Monash University course entry'),
  ('monash university','direct-master','Direct Master','Master','https://www.monash.edu/study/courses','Monash University postgraduate course entry'),
  ('monash university','foundation-bachelor','Foundation + Bachelor','Bachelor','https://www.monashcollege.edu.au/__data/assets/pdf_file/0015/4114311/Monash-Pathway-Programs-2026.pdf','Official 2026 Monash pathway guide'),
  ('monash university','diploma-bachelor','Diploma + Bachelor','Bachelor','https://www.monashcollege.edu.au/study/courses/diplomas','Official Monash College diploma progression'),
  ('monash university','foundation-diploma-bachelor','Foundation + Diploma + Bachelor','Bachelor','https://www.monashcollege.edu.au/study/courses/foundation-year/destination-degrees/entry-requirements-foundation-year-to-diploma','Official Foundation to Diploma progression'),
  ('deakin university','direct-bachelor','Direct Bachelor','Bachelor','https://www.deakin.edu.au/courses','Deakin University course entry'),
  ('deakin university','direct-master','Direct Master','Master','https://www.deakin.edu.au/courses','Deakin University postgraduate course entry'),
  ('deakin university','foundation-bachelor','Foundation + Bachelor','Bachelor','https://www.deakincollege.edu.au/courses/foundation-program/','Official Foundation to first-year degree pathway'),
  ('deakin university','diploma-bachelor','Diploma + Bachelor','Bachelor','https://www.deakincollege.edu.au/courses/diploma/','Official Diploma to Deakin degree pathway'),
  ('deakin university','foundation-diploma-bachelor','Foundation + Diploma + Bachelor','Bachelor','https://www.deakincollege.edu.au/courses/foundation-program/','Official Foundation to Diploma to degree pathway'),
  ('university of sydney','direct-bachelor','Direct Bachelor','Bachelor','https://www.sydney.edu.au/courses/','University of Sydney course entry'),
  ('university of sydney','direct-master','Direct Master','Master','https://www.sydney.edu.au/courses/','University of Sydney postgraduate course entry'),
  ('university of sydney','foundation-bachelor','Foundation + Bachelor','Bachelor','https://www.sydney.edu.au/study/applying/how-to-apply/international-students/preparation-programs.html','Official preparation program pathway'),
  ('university of new south wales','direct-bachelor','Direct Bachelor','Bachelor','https://www.unsw.edu.au/study','UNSW course entry'),
  ('university of new south wales','direct-master','Direct Master','Master','https://www.unsw.edu.au/study','UNSW postgraduate course entry'),
  ('university of new south wales','foundation-bachelor','Foundation + Bachelor','Bachelor','https://www.unswcollege.edu.au/study/foundation-studies-overview','Official Foundation Studies pathway'),
  ('university of new south wales','diploma-bachelor','Diploma + Bachelor','Bachelor','https://www.unswcollege.edu.au/study/diploma-pathways','Official Diploma to second-year pathway')
)
INSERT OR IGNORE INTO higher_education_pathway_combinations(
  university_id,pathway_code,display_name,final_course_level,active_status,source_url,evidence,created_at,updated_at
)
SELECT u.id,s.pathway_code,s.display_name,s.final_course_level,'Active',s.source_url,s.evidence,'2026-08-13','2026-08-13'
FROM pathway_seed s JOIN higher_education_providers u ON u.normalized_name=s.university_name;

WITH segment_seed(university_name,pathway_code,segment_order,provider_name,course_level) AS (
  VALUES
  ('monash university','direct-bachelor',1,'monash university','Bachelor'),
  ('monash university','direct-master',1,'monash university','Master'),
  ('monash university','foundation-bachelor',1,'monash college','Foundation'),
  ('monash university','foundation-bachelor',2,'monash university','Bachelor'),
  ('monash university','diploma-bachelor',1,'monash college','Diploma'),
  ('monash university','diploma-bachelor',2,'monash university','Bachelor'),
  ('monash university','foundation-diploma-bachelor',1,'monash college','Foundation'),
  ('monash university','foundation-diploma-bachelor',2,'monash college','Diploma'),
  ('monash university','foundation-diploma-bachelor',3,'monash university','Bachelor'),
  ('deakin university','direct-bachelor',1,'deakin university','Bachelor'),
  ('deakin university','direct-master',1,'deakin university','Master'),
  ('deakin university','foundation-bachelor',1,'deakin college','Foundation'),
  ('deakin university','foundation-bachelor',2,'deakin university','Bachelor'),
  ('deakin university','diploma-bachelor',1,'deakin college','Diploma'),
  ('deakin university','diploma-bachelor',2,'deakin university','Bachelor'),
  ('deakin university','foundation-diploma-bachelor',1,'deakin college','Foundation'),
  ('deakin university','foundation-diploma-bachelor',2,'deakin college','Diploma'),
  ('deakin university','foundation-diploma-bachelor',3,'deakin university','Bachelor'),
  ('university of sydney','direct-bachelor',1,'university of sydney','Bachelor'),
  ('university of sydney','direct-master',1,'university of sydney','Master'),
  ('university of sydney','foundation-bachelor',1,'taylors college sydney','Foundation'),
  ('university of sydney','foundation-bachelor',2,'university of sydney','Bachelor'),
  ('university of new south wales','direct-bachelor',1,'university of new south wales','Bachelor'),
  ('university of new south wales','direct-master',1,'university of new south wales','Master'),
  ('university of new south wales','foundation-bachelor',1,'unsw college','Foundation'),
  ('university of new south wales','foundation-bachelor',2,'university of new south wales','Bachelor'),
  ('university of new south wales','diploma-bachelor',1,'unsw college','Diploma'),
  ('university of new south wales','diploma-bachelor',2,'university of new south wales','Bachelor')
)
INSERT OR IGNORE INTO higher_education_pathway_segments(
  pathway_combination_id,segment_order,provider_id,course_level,target_university_id,
  source_url,evidence,created_at,updated_at
)
SELECT c.id,s.segment_order,p.id,s.course_level,u.id,c.source_url,c.evidence,'2026-08-13','2026-08-13'
FROM segment_seed s
JOIN higher_education_providers u ON u.normalized_name=s.university_name
JOIN higher_education_pathway_combinations c ON c.university_id=u.id AND c.pathway_code=s.pathway_code
JOIN higher_education_providers p ON p.normalized_name=s.provider_name;
