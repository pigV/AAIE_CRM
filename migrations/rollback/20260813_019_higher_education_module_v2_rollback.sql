-- Run only against a verified backup after confirming no V2 rows are required.
DROP TRIGGER IF EXISTS higher_education_application_course_scope_update;
DROP TRIGGER IF EXISTS higher_education_application_course_scope_insert;
DROP TRIGGER IF EXISTS higher_education_application_v2_scope_update;
DROP TRIGGER IF EXISTS higher_education_application_v2_scope_insert;
DROP TRIGGER IF EXISTS higher_education_plan_item_course_scope_update;
DROP TRIGGER IF EXISTS higher_education_plan_item_course_scope_insert;
DROP TRIGGER IF EXISTS higher_education_plan_item_v2_scope_update;
DROP TRIGGER IF EXISTS higher_education_plan_item_v2_scope_insert;
DROP TRIGGER IF EXISTS higher_education_course_university_update;
DROP TRIGGER IF EXISTS higher_education_course_university_insert;
DROP TRIGGER IF EXISTS higher_education_pathway_segment_scope_update;
DROP TRIGGER IF EXISTS higher_education_pathway_segment_scope_insert;
DROP TRIGGER IF EXISTS higher_education_pathway_university_update;
DROP TRIGGER IF EXISTS higher_education_pathway_university_insert;
ALTER TABLE higher_education_package_segments DROP COLUMN is_final_degree;
ALTER TABLE higher_education_package_segments DROP COLUMN course_id;
ALTER TABLE higher_education_package_segments DROP COLUMN target_university_id;
ALTER TABLE higher_education_applications DROP COLUMN course_id;
ALTER TABLE higher_education_applications DROP COLUMN pathway_combination_id;
ALTER TABLE higher_education_plan_items DROP COLUMN course_id;
ALTER TABLE higher_education_plan_items DROP COLUMN pathway_combination_id;
ALTER TABLE higher_education_plans DROP COLUMN default_pathway_code;
DROP TABLE IF EXISTS higher_education_courses;
DROP TABLE IF EXISTS higher_education_pathway_segments;
DROP TABLE IF EXISTS higher_education_pathway_combinations;
DELETE FROM higher_education_provider_relationships
WHERE (university_id=(SELECT id FROM higher_education_providers WHERE normalized_name='deakin university')
       AND pathway_provider_id=(SELECT id FROM higher_education_providers WHERE normalized_name='deakin college'))
   OR (university_id=(SELECT id FROM higher_education_providers WHERE normalized_name='university of sydney')
       AND pathway_provider_id=(SELECT id FROM higher_education_providers WHERE normalized_name='taylors college sydney'));
DELETE FROM higher_education_providers
WHERE normalized_name IN ('deakin college','taylors college sydney');
ALTER TABLE higher_education_providers DROP COLUMN city;
ALTER TABLE higher_education_providers DROP COLUMN state;
ALTER TABLE higher_education_providers DROP COLUMN short_name;
ALTER TABLE students DROP COLUMN parent_requested_pathway;
ALTER TABLE students DROP COLUMN higher_education_package_combination;
